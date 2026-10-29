/**
 * WebSocket do player (RDPP §4) — sem DOM (fábrica de WebSocket injetável).
 *  - Conexão por ticket de uso único (30 s) obtido via POST /ws-ticket (Bearer); nenhum token na URL.
 *  - Envelope `{type,id,ts_ms,data}`, tipos minúsculos com "_".
 *  - Heartbeat a cada `heartbeat_interval_ms` (30 s), telemetria a cada `telemetry_interval_ms` (5 min) ou sob demanda.
 *  - Reconexão com backoff exponencial + jitter (1 s → 60 s). Na (re)conexão o app busca o manifesto e envia o outbox.
 */

import type { CommandData, Envelope, HeartbeatData, KillData, TelemetryData } from './protocol';
import { backoffDelay, errorMessage, realTimers, uuidv4, type Timers } from './util';

export interface WebSocketLike {
  readonly readyState: number;
  send(data: string): void;
  close(code?: number, reason?: string): void;
  onopen: ((ev: unknown) => void) | null;
  onclose: ((ev: { code: number; reason: string }) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
}

export type WebSocketFactory = (url: string) => WebSocketLike;

export const WS_OPEN = 1;

export function makeEnvelope<T>(type: string, data: T, nowMs: number, id: string = uuidv4()): Envelope<T> {
  return { type, id, ts_ms: Math.trunc(nowMs), data };
}

/** Faz o parse tolerante de uma mensagem do servidor; null se não for um envelope válido. */
export function parseEnvelope(raw: unknown): Envelope | null {
  let v: unknown = raw;
  if (typeof raw === 'string') {
    try {
      v = JSON.parse(raw);
    } catch {
      return null;
    }
  }
  if (typeof v !== 'object' || v === null || Array.isArray(v)) return null;
  const o = v as Record<string, unknown>;
  if (typeof o.type !== 'string' || !/^[a-z][a-z0-9_]*$/.test(o.type)) return null;
  const data = typeof o.data === 'object' && o.data !== null && !Array.isArray(o.data) ? (o.data as Record<string, unknown>) : {};
  return {
    type: o.type,
    id: typeof o.id === 'string' ? o.id : '',
    ts_ms: typeof o.ts_ms === 'number' ? o.ts_ms : 0,
    data,
  };
}

export interface CloseInfo {
  code: number;
  reason: string;
  /** Atraso até a próxima tentativa (ms), ou null se parado. */
  retryInMs: number | null;
}

export interface ReconnectingSocketDeps {
  /** Monta a URL (pede ticket novo a cada conexão). Erro ⇒ conta como falha de conexão. */
  url: () => Promise<string>;
  factory: WebSocketFactory;
  timers?: Timers;
  random?: () => number;
  baseDelayMs?: number;
  maxDelayMs?: number;
  onOpen?: () => void;
  onMessage?: (env: Envelope) => void;
  onClose?: (info: CloseInfo) => void;
  log?: (msg: string) => void;
}

export class ReconnectingSocket {
  private ws: WebSocketLike | null = null;
  private attempt = 0;
  private running = false;
  private retryTimer: unknown = null;
  private readonly timers: Timers;
  private readonly random: () => number;
  private readonly base: number;
  private readonly max: number;
  /** Atrasos efetivamente agendados (diagnóstico/testes). */
  readonly scheduledDelays: number[] = [];

  constructor(private readonly deps: ReconnectingSocketDeps) {
    this.timers = deps.timers ?? realTimers;
    this.random = deps.random ?? Math.random;
    this.base = deps.baseDelayMs ?? 1_000;
    this.max = deps.maxDelayMs ?? 60_000;
  }

  get connected(): boolean {
    return this.ws !== null && this.ws.readyState === WS_OPEN;
  }

  start(): void {
    if (this.running) return;
    this.running = true;
    this.attempt = 0;
    void this.connect();
  }

  stop(): void {
    this.running = false;
    if (this.retryTimer !== null) this.timers.clearTimeout(this.retryTimer);
    this.retryTimer = null;
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      ws.onclose = null;
      ws.onmessage = null;
      ws.onerror = null;
      try {
        ws.close(1000, 'stop');
      } catch {
        /* ignore */
      }
    }
  }

  /** Força reconexão imediata (ex.: retorno de suspensão, rede voltou). */
  kick(): void {
    if (!this.running || this.connected) return;
    if (this.retryTimer !== null) this.timers.clearTimeout(this.retryTimer);
    this.retryTimer = null;
    void this.connect();
  }

  send<T>(env: Envelope<T>): boolean {
    if (!this.connected || !this.ws) return false;
    try {
      this.ws.send(JSON.stringify(env));
      return true;
    } catch {
      return false;
    }
  }

  private async connect(): Promise<void> {
    if (!this.running) return;
    let url: string;
    try {
      url = await this.deps.url();
    } catch (e) {
      this.deps.log?.(`ws: falha ao obter ticket: ${errorMessage(e)}`);
      this.scheduleRetry(0, 'ticket');
      return;
    }
    if (!this.running) return;
    let ws: WebSocketLike;
    try {
      ws = this.deps.factory(url);
    } catch (e) {
      this.deps.log?.(`ws: falha ao abrir: ${errorMessage(e)}`);
      this.scheduleRetry(0, 'open');
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      this.attempt = 0;
      this.deps.onOpen?.();
    };
    ws.onmessage = (ev) => {
      const env = parseEnvelope(ev.data);
      if (env) this.deps.onMessage?.(env);
    };
    ws.onerror = () => {
      /* o close vem em seguida */
    };
    ws.onclose = (ev) => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.scheduleRetry(ev?.code ?? 0, ev?.reason ?? '');
    };
  }

  private scheduleRetry(code: number, reason: string): void {
    if (!this.running) {
      this.deps.onClose?.({ code, reason, retryInMs: null });
      return;
    }
    const delay = backoffDelay(this.attempt, this.base, this.max, this.random);
    this.attempt++;
    this.scheduledDelays.push(delay);
    this.deps.onClose?.({ code, reason, retryInMs: delay });
    this.retryTimer = this.timers.setTimeout(() => {
      this.retryTimer = null;
      void this.connect();
    }, delay);
  }
}

// ───────────────────────── Canal do player ─────────────────────────

export interface ChannelHandlers {
  heartbeat: () => HeartbeatData;
  telemetry: () => Promise<TelemetryData>;
  onServerTime?: (serverMs: number) => void;
  onSyncRequired?: (version: number | null) => void;
  onCommand?: (cmd: CommandData) => void;
  onKill?: (kill: KillData) => void;
  onRevoked?: () => void;
  onFlush?: () => void;
  onConnected?: () => void;
  onDisconnected?: (info: CloseInfo) => void;
  /** Qualquer mensagem recebida (contato com o servidor). */
  onContact?: () => void;
}

export interface ChannelSettings {
  heartbeat_interval_ms: number;
  telemetry_interval_ms: number;
}

export interface PlayerChannelDeps {
  url: () => Promise<string>;
  factory: WebSocketFactory;
  handlers: ChannelHandlers;
  settings: () => ChannelSettings;
  now: () => number;
  timers?: Timers;
  random?: () => number;
  uuid?: () => string;
  log?: (msg: string) => void;
}

export class PlayerChannel {
  readonly socket: ReconnectingSocket;
  private hbTimer: unknown = null;
  private telTimer: unknown = null;
  private readonly timers: Timers;
  private readonly uuid: () => string;

  constructor(private readonly deps: PlayerChannelDeps) {
    this.timers = deps.timers ?? realTimers;
    this.uuid = deps.uuid ?? (() => uuidv4());
    this.socket = new ReconnectingSocket({
      url: deps.url,
      factory: deps.factory,
      timers: this.timers,
      random: deps.random,
      log: deps.log,
      onOpen: () => this.handleOpen(),
      onMessage: (env) => this.handleMessage(env),
      onClose: (info) => {
        this.stopTimers();
        deps.handlers.onDisconnected?.(info);
      },
    });
  }

  get connected(): boolean {
    return this.socket.connected;
  }

  start(): void {
    this.socket.start();
  }

  stop(): void {
    this.stopTimers();
    this.socket.stop();
  }

  send<T>(type: string, data: T): boolean {
    return this.socket.send(makeEnvelope(type, data, this.deps.now(), this.uuid()));
  }

  sendHeartbeat(): boolean {
    return this.send('heartbeat', this.deps.handlers.heartbeat());
  }

  async sendTelemetry(): Promise<boolean> {
    try {
      const t = await this.deps.handlers.telemetry();
      return this.send('telemetry', t);
    } catch (e) {
      this.deps.log?.(`telemetria: ${errorMessage(e)}`);
      return false;
    }
  }

  private handleOpen(): void {
    this.deps.handlers.onContact?.();
    this.sendHeartbeat();
    void this.sendTelemetry();
    this.scheduleHeartbeat();
    this.scheduleTelemetry();
    this.deps.handlers.onConnected?.();
  }

  private scheduleHeartbeat(): void {
    if (this.hbTimer !== null) this.timers.clearTimeout(this.hbTimer);
    const ms = Math.max(1_000, this.deps.settings().heartbeat_interval_ms || 30_000);
    this.hbTimer = this.timers.setTimeout(() => {
      this.hbTimer = null;
      if (!this.connected) return;
      this.sendHeartbeat();
      this.deps.handlers.onContact?.();
      this.scheduleHeartbeat();
    }, ms);
  }

  private scheduleTelemetry(): void {
    if (this.telTimer !== null) this.timers.clearTimeout(this.telTimer);
    const ms = Math.max(10_000, this.deps.settings().telemetry_interval_ms || 300_000);
    this.telTimer = this.timers.setTimeout(() => {
      this.telTimer = null;
      if (!this.connected) return;
      void this.sendTelemetry();
      this.scheduleTelemetry();
    }, ms);
  }

  private stopTimers(): void {
    if (this.hbTimer !== null) this.timers.clearTimeout(this.hbTimer);
    if (this.telTimer !== null) this.timers.clearTimeout(this.telTimer);
    this.hbTimer = null;
    this.telTimer = null;
  }

  private handleMessage(env: Envelope): void {
    const h = this.deps.handlers;
    h.onContact?.();
    const d = env.data;
    switch (env.type) {
      case 'server_time': {
        const ms = Number(d.server_time_ms);
        if (Number.isFinite(ms)) h.onServerTime?.(ms);
        break;
      }
      case 'sync_required': {
        const v = Number(d.version);
        h.onSyncRequired?.(Number.isFinite(v) ? v : null);
        break;
      }
      case 'command': {
        if (typeof d.command_id !== 'string' || typeof d.command !== 'string') break;
        h.onCommand?.({
          command_id: d.command_id,
          command: d.command,
          args: typeof d.args === 'object' && d.args !== null ? (d.args as Record<string, unknown>) : null,
          expires_at_ms: typeof d.expires_at_ms === 'number' ? d.expires_at_ms : null,
        });
        break;
      }
      case 'kill': {
        const ids = Array.isArray(d.creative_ids) ? d.creative_ids.filter((x): x is string => typeof x === 'string') : [];
        h.onKill?.({ creative_ids: ids, reason: typeof d.reason === 'string' ? d.reason : null });
        break;
      }
      case 'device_revoked':
        h.onRevoked?.();
        break;
      case 'flush_events':
        h.onFlush?.();
        break;
      default:
        this.deps.log?.(`ws: tipo desconhecido ignorado: ${env.type}`);
    }
  }
}
