/**
 * Conexão WebSocket do painel (contrato v1 §5) — sem polling.
 *
 *  1. POST /realtime/ticket (Bearer) → ticket de uso único (30 s)
 *  2. WS /ws/admin?ticket=<ticket>  (único segredo permitido em query string: uso único)
 *  3. Queda → reconexão com backoff exponencial + jitter (teto 30 s). Cada tentativa pede
 *     um ticket novo.
 *  4. Ao reabrir após qualquer queda/falha → `onResync()` UMA vez (o provider refaz só as
 *     queries ativas).
 *  404/403 no ticket = recurso indisponível para este usuário/ambiente → para de tentar.
 */
import { isApiError } from '@/lib/errors'
import type { RealtimeEnvelope } from './eventMap'

export type RealtimeStatus = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'unavailable' | 'closed'

export interface RealtimeConnectionOptions {
  getTicket: () => Promise<string>
  buildUrl: (ticket: string) => string
  onEvent: (event: RealtimeEnvelope) => void
  onStatus: (status: RealtimeStatus) => void
  onResync: () => void
  WebSocketImpl?: typeof WebSocket
  random?: () => number
  baseDelayMs?: number
  maxDelayMs?: number
}

/** Backoff exponencial com "equal jitter": metade fixa + metade aleatória do teto da tentativa. */
export function computeBackoff(attempt: number, baseMs = 1000, maxMs = 30_000, random: () => number = Math.random) {
  const cap = Math.min(maxMs, baseMs * 2 ** Math.max(0, attempt))
  return Math.round(cap / 2 + random() * (cap / 2))
}

const MAX_SEEN_IDS = 500

export class RealtimeConnection {
  private ws: WebSocket | null = null
  private timer: ReturnType<typeof setTimeout> | null = null
  private attempt = 0
  private stopped = true
  private needsResync = false
  private seen: string[] = []
  private seenSet = new Set<string>()
  private status: RealtimeStatus = 'idle'
  private readonly opts: Required<Omit<RealtimeConnectionOptions, 'WebSocketImpl'>> & {
    WebSocketImpl?: typeof WebSocket
  }

  constructor(opts: RealtimeConnectionOptions) {
    this.opts = {
      random: Math.random,
      baseDelayMs: 1000,
      maxDelayMs: 30_000,
      ...opts,
    }
  }

  get currentStatus() {
    return this.status
  }

  start() {
    if (!this.stopped) return
    this.stopped = false
    if (typeof window !== 'undefined') window.addEventListener('online', this.handleOnline)
    void this.connect()
  }

  stop() {
    this.stopped = true
    if (typeof window !== 'undefined') window.removeEventListener('online', this.handleOnline)
    if (this.timer) clearTimeout(this.timer)
    this.timer = null
    const ws = this.ws
    this.ws = null
    if (ws) {
      ws.onopen = ws.onclose = ws.onmessage = ws.onerror = null
      try {
        ws.close(1000, 'client-stop')
      } catch {
        /* ignora */
      }
    }
    this.setStatus('closed')
  }

  private setStatus(s: RealtimeStatus) {
    if (this.status === s) return
    this.status = s
    this.opts.onStatus(s)
  }

  private handleOnline = () => {
    if (this.stopped || this.ws) return
    if (this.timer) clearTimeout(this.timer)
    this.timer = null
    void this.connect()
  }

  private async connect() {
    if (this.stopped) return
    this.setStatus(this.attempt === 0 && !this.needsResync ? 'connecting' : 'reconnecting')
    let ticket: string
    try {
      ticket = await this.opts.getTicket()
    } catch (e) {
      if (this.stopped) return
      if (isApiError(e) && (e.status === 404 || e.status === 403 || e.status === 405)) {
        this.setStatus('unavailable')
        this.stopped = true
        return
      }
      if (isApiError(e) && e.status === 401) {
        // Sessão expirada: o cliente HTTP já notificou o AuthProvider.
        this.setStatus('closed')
        this.stopped = true
        return
      }
      this.needsResync = true
      this.scheduleReconnect()
      return
    }
    if (this.stopped) return

    const Impl = this.opts.WebSocketImpl ?? WebSocket
    let ws: WebSocket
    try {
      ws = new Impl(this.opts.buildUrl(ticket))
    } catch {
      this.needsResync = true
      this.scheduleReconnect()
      return
    }
    this.ws = ws

    ws.onopen = () => {
      if (this.stopped) return
      this.attempt = 0
      this.setStatus('open')
      if (this.needsResync) {
        this.needsResync = false
        this.opts.onResync()
      }
    }
    ws.onmessage = (ev: MessageEvent) => {
      const env = this.parse(ev.data)
      if (!env) return
      if (env.id) {
        if (this.seenSet.has(env.id)) return
        this.seenSet.add(env.id)
        this.seen.push(env.id)
        if (this.seen.length > MAX_SEEN_IDS) {
          const old = this.seen.shift()
          if (old) this.seenSet.delete(old)
        }
      }
      this.opts.onEvent(env)
    }
    ws.onerror = () => {
      /* o onclose seguinte decide a reconexão */
    }
    ws.onclose = () => {
      if (this.ws === ws) this.ws = null
      if (this.stopped) return
      this.needsResync = true
      this.scheduleReconnect()
    }
  }

  private parse(raw: unknown): RealtimeEnvelope | null {
    if (typeof raw !== 'string') return null
    try {
      const v = JSON.parse(raw) as RealtimeEnvelope
      if (!v || typeof v.type !== 'string') return null
      if (v.type === 'pong') return null
      return v
    } catch {
      return null
    }
  }

  private scheduleReconnect() {
    if (this.stopped) return
    this.setStatus('reconnecting')
    const delay = computeBackoff(this.attempt, this.opts.baseDelayMs, this.opts.maxDelayMs, this.opts.random)
    this.attempt += 1
    if (this.timer) clearTimeout(this.timer)
    this.timer = setTimeout(() => {
      this.timer = null
      void this.connect()
    }, delay)
  }
}
