/**
 * Cliente HTTP do painel — contrato v1 §1–§3.
 *
 * - Access token SOMENTE em memória (nada em localStorage/sessionStorage).
 * - Refresh por cookie HttpOnly `rd_refresh` (Path=/api/v1/auth): `POST /auth/refresh` com
 *   `X-RD-CSRF: 1` e `credentials: 'include'`. Apenas as rotas que leem/gravam o cookie usam
 *   credenciais (refresh, logout e as respostas que emitem o cookie: login, mfa/verify, mfa/totp/enable,
 *   invites/accept — sem `include` o navegador ignora o Set-Cookie). Demais rotas: `omit`.
 * - 401 → UMA tentativa de refresh compartilhada por todas as requisições concorrentes (fila),
 *   depois repete a requisição original uma única vez. Rotas de autenticação nunca disparam refresh
 *   (o 401 do próprio login chega intacto à tela, sem redirecionamento).
 * - Refresh com ROTAÇÃO ESTRITA (reusar o cookie antigo revoga a sessão): a renovação é
 *   serializada também ENTRE ABAS (Web Locks `rd-auth-refresh`) e o token novo é repassado às
 *   outras abas por BroadcastChannel `rd-auth` — quem estava esperando o lock reaproveita o token
 *   em vez de girar o cookie de novo. Logout também é avisado às outras abas.
 * - Envia `X-Request-ID` em toda chamada e `X-RD-Context` com o contexto ativo.
 * - Erros sempre como `ApiError` (envelope do contrato).
 */
import { ApiError, toApiError } from './errors'

export const API_BASE = '/api/v1'

// ---------------------------------------------------------------------------
// Token em memória
// ---------------------------------------------------------------------------
let accessToken: string | null = null
type Listener = () => void
const sessionExpiredListeners = new Set<Listener>()

export const tokenStore = {
  get: () => accessToken,
  set: (t: string | null) => {
    accessToken = t
  },
  clear: () => {
    accessToken = null
  },
}

/** Notificado quando a sessão não pode mais ser renovada (refresh falhou após um 401). */
export function onSessionExpired(fn: Listener): () => void {
  sessionExpiredListeners.add(fn)
  return () => sessionExpiredListeners.delete(fn)
}

// ---------------------------------------------------------------------------
// Coordenação entre abas (mesma origem)
// ---------------------------------------------------------------------------
type AuthMessage = { type: 'token'; access_token: string } | { type: 'logout' }
const remoteLogoutListeners = new Set<Listener>()
let channel: BroadcastChannel | null = null

function getChannel(): BroadcastChannel | null {
  if (channel || typeof BroadcastChannel === 'undefined') return channel
  channel = new BroadcastChannel('rd-auth')
  channel.onmessage = (ev: MessageEvent<AuthMessage>) => {
    const msg = ev.data
    if (msg?.type === 'token' && typeof msg.access_token === 'string') {
      // Só adota se esta aba está esperando uma renovação (não troca o token de uma aba em
      // impersonation nem "loga" uma aba anônima).
      if (refreshInFlight) accessToken = msg.access_token
    } else if (msg?.type === 'logout') {
      accessToken = null
      remoteLogoutListeners.forEach((fn) => fn())
    }
  }
  return channel
}

/** Avisa as outras abas que a sessão foi encerrada (logout / sair de todos). */
export function broadcastLogout() {
  try {
    getChannel()?.postMessage({ type: 'logout' } satisfies AuthMessage)
  } catch {
    /* canal indisponível */
  }
}

const tokenRefreshedListeners = new Set<Listener>()
/** Notificado após cada renovação bem-sucedida (ex.: impersonation expirada volta ao usuário real). */
export function onTokenRefreshed(fn: Listener): () => void {
  tokenRefreshedListeners.add(fn)
  return () => tokenRefreshedListeners.delete(fn)
}

/** Notificado quando OUTRA aba encerrou a sessão. */
export function onRemoteLogout(fn: Listener): () => void {
  getChannel()
  remoteLogoutListeners.add(fn)
  return () => remoteLogoutListeners.delete(fn)
}

function withRefreshLock<T>(fn: () => Promise<T>): Promise<T> {
  const locks = (globalThis.navigator as Navigator | undefined)?.locks
  if (locks && typeof locks.request === 'function') return locks.request('rd-auth-refresh', fn) as Promise<T>
  return fn()
}

// ---------------------------------------------------------------------------
// Contexto ativo (X-RD-Context) — filtro de UX; autorização é do servidor
// ---------------------------------------------------------------------------
let activeContext: string | null = null
export function setActiveContextHeader(key: string | null) {
  activeContext = key
}
export function getActiveContextHeader() {
  return activeContext
}

// ---------------------------------------------------------------------------
// Utilidades
// ---------------------------------------------------------------------------
export function newRequestId(): string {
  const c = globalThis.crypto as Crypto | undefined
  if (c && typeof c.randomUUID === 'function') return c.randomUUID()
  // Fallback RFC4122 v4 com getRandomValues
  const bytes = new Uint8Array(16)
  if (c && typeof c.getRandomValues === 'function') c.getRandomValues(bytes)
  else for (let i = 0; i < 16; i++) bytes[i] = Math.floor(Math.random() * 256)
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const h = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`
}

/** Rotas cujo cookie de refresh é lido ou emitido pelo servidor. */
const COOKIE_ROUTES = [
  '/auth/refresh',
  '/auth/logout',
  '/auth/login',
  '/auth/mfa/verify',
  '/auth/mfa/totp/enable', // com mfa_setup_token devolve a sessão completa + cookie
  '/auth/invites/accept',
  '/auth/master-activation/complete', // AÇÃO 003: ativação do 2º Master devolve a sessão + cookie
]
/** Rotas que exigem o cabeçalho anti-CSRF (usam o cookie como credencial). */
const CSRF_ROUTES = ['/auth/refresh', '/auth/logout']
/** Rotas em que 401 é resposta de negócio (credencial/código inválido) e NUNCA dispara refresh. */
const NO_REFRESH_ROUTES = [
  '/auth/login',
  '/auth/mfa/verify',
  '/auth/refresh',
  '/auth/logout',
  '/auth/password/forgot',
  '/auth/password/reset',
  '/auth/invites/accept',
]

const matches = (list: string[], path: string) => list.some((p) => path === p || path.startsWith(`${p}?`))

export type QueryValue = string | number | boolean | null | undefined | Array<string | number>
export type QueryParams = Record<string, QueryValue>

export function buildUrl(path: string, query?: QueryParams): string {
  const url = `${API_BASE}${path.startsWith('/') ? path : `/${path}`}`
  if (!query) return url
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === '') continue
    if (Array.isArray(v)) v.forEach((item) => sp.append(k, String(item)))
    else sp.set(k, String(v))
  }
  const qs = sp.toString()
  return qs ? `${url}?${qs}` : url
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  body?: unknown
  query?: QueryParams
  headers?: Record<string, string>
  signal?: AbortSignal
  /** Envia o Bearer (padrão true). */
  auth?: boolean
  /** Tenta refresh + repetição em 401 (padrão true, exceto rotas de auth). */
  retryOn401?: boolean
  /** Header Idempotency-Key para operações sensíveis (checkout, saque, aprovação, comandos). */
  idempotencyKey?: string
  /** false = não envia X-RD-Context nesta chamada. */
  sendContext?: boolean
}

// ---------------------------------------------------------------------------
// Refresh com fila (single-flight)
// ---------------------------------------------------------------------------
let refreshInFlight: Promise<string | null> | null = null

async function doRefresh(stale: string | null): Promise<string | null> {
  return withRefreshLock(async () => {
    // Outra aba (ou outro fluxo) renovou enquanto esperávamos o lock: reaproveita, não gira de novo.
    if (accessToken && accessToken !== stale) return accessToken
    try {
      const res = await fetch(buildUrl('/auth/refresh'), {
        method: 'POST',
        credentials: 'include',
        headers: { Accept: 'application/json', 'X-RD-CSRF': '1', 'X-Request-ID': newRequestId() },
      })
      if (!res.ok) {
        tokenStore.clear()
        return null
      }
      const data = (await res.json()) as { access_token?: string }
      if (!data.access_token) {
        tokenStore.clear()
        return null
      }
      tokenStore.set(data.access_token)
      try {
        getChannel()?.postMessage({ type: 'token', access_token: data.access_token } satisfies AuthMessage)
      } catch {
        /* canal indisponível */
      }
      tokenRefreshedListeners.forEach((fn) => fn())
      return data.access_token
    } catch {
      // Falha de rede: não derruba a sessão em memória por si só
      return null
    }
  })
}

/**
 * Renova o access token. Chamadas concorrentes da aba compartilham a MESMA requisição e,
 * entre abas, o lock garante uma renovação por vez (rotação estrita do refresh).
 */
export function refreshAccessToken(): Promise<string | null> {
  if (!refreshInFlight) {
    const stale = accessToken
    refreshInFlight = doRefresh(stale).finally(() => {
      refreshInFlight = null
    })
  }
  return refreshInFlight
}

// ---------------------------------------------------------------------------
// Requisição
// ---------------------------------------------------------------------------
async function parseBody(res: Response): Promise<unknown> {
  if (res.status === 204 || res.status === 205) {
    // Consome o corpo vazio (evita a requisição aparecer como "abortada" nas ferramentas do navegador).
    await res.text().catch(() => undefined)
    return undefined
  }
  const ct = res.headers.get('content-type') ?? ''
  if (ct.includes('application/json')) {
    try {
      return await res.json()
    } catch {
      return undefined
    }
  }
  const text = await res.text().catch(() => '')
  return text || undefined
}

export async function request<T = unknown>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, query, signal, auth = true, idempotencyKey, sendContext = true } = opts
  const retryOn401 = opts.retryOn401 ?? !matches(NO_REFRESH_ROUTES, path)
  const requestId = newRequestId()

  const headers: Record<string, string> = { Accept: 'application/json', 'X-Request-ID': requestId, ...opts.headers }
  const isForm = typeof FormData !== 'undefined' && body instanceof FormData
  if (body !== undefined && !isForm) headers['Content-Type'] = 'application/json'
  if (auth && accessToken) headers.Authorization = `Bearer ${accessToken}`
  if (sendContext && activeContext) headers['X-RD-Context'] = activeContext
  if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey
  if (matches(CSRF_ROUTES, path)) headers['X-RD-CSRF'] = '1'

  let res: Response
  try {
    res = await fetch(buildUrl(path, query), {
      method,
      headers,
      body: body === undefined ? undefined : isForm ? (body as FormData) : JSON.stringify(body),
      credentials: matches(COOKIE_ROUTES, path) ? 'include' : 'omit',
      signal,
    })
  } catch (err) {
    if ((err as { name?: string })?.name === 'AbortError') throw err
    throw toApiError(0, undefined, { requestId })
  }

  if (res.status === 401 && auth && retryOn401) {
    const tokenUsed = headers.Authorization
    // Se outro fluxo já renovou o token enquanto esta requisição estava em voo, só repete.
    const current = accessToken ? `Bearer ${accessToken}` : undefined
    const newToken = current && current !== tokenUsed ? accessToken : await refreshAccessToken()
    if (newToken) {
      return request<T>(path, { ...opts, retryOn401: false })
    }
    sessionExpiredListeners.forEach((fn) => fn())
  }

  const data = await parseBody(res)
  if (!res.ok) {
    throw toApiError(res.status, data, {
      requestId: res.headers.get('x-request-id') ?? requestId,
      retryAfterHeader: res.headers.get('retry-after'),
    })
  }
  return data as T
}

export const http = {
  get: <T>(path: string, opts?: Omit<RequestOptions, 'method' | 'body'>) => request<T>(path, { ...opts, method: 'GET' }),
  post: <T>(path: string, body?: unknown, opts?: Omit<RequestOptions, 'method' | 'body'>) =>
    request<T>(path, { ...opts, method: 'POST', body }),
  put: <T>(path: string, body?: unknown, opts?: Omit<RequestOptions, 'method' | 'body'>) =>
    request<T>(path, { ...opts, method: 'PUT', body }),
  patch: <T>(path: string, body?: unknown, opts?: Omit<RequestOptions, 'method' | 'body'>) =>
    request<T>(path, { ...opts, method: 'PATCH', body }),
  delete: <T>(path: string, opts?: Omit<RequestOptions, 'method' | 'body'>) =>
    request<T>(path, { ...opts, method: 'DELETE' }),
}

/** Resposta paginada do contrato §1. */
export interface Paginated<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

export { ApiError }
