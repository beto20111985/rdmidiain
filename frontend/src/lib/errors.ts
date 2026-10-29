/**
 * Erro padronizado do contrato v1 (docs/contracts/API_CONVENCOES_AUTH_RBAC_REALTIME_v1.md §1):
 *   {"error": {"code": "...", "message": "Mensagem pt-BR", "details": {}}}
 * Também aceita, de forma transitória, o formato legado do FastAPI ({"detail": ...}).
 */
export type FieldErrors = Record<string, string>

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details: Record<string, unknown>
  readonly requestId?: string

  constructor(init: {
    status: number
    code: string
    message: string
    details?: Record<string, unknown>
    requestId?: string
  }) {
    super(init.message)
    this.name = 'ApiError'
    this.status = init.status
    this.code = init.code
    this.details = init.details ?? {}
    this.requestId = init.requestId
  }

  /** Segundos até poder tentar de novo (423 bloqueio, 429 rate limit). */
  get retryAfterSeconds(): number | null {
    const v = this.details.retry_after_s
    return typeof v === 'number' && Number.isFinite(v) ? v : null
  }

  /** Erros por campo (422 / 400 de validação) no formato { campo: mensagem }. */
  get fieldErrors(): FieldErrors {
    // Formato do backend v1 para 422: details.errors = [{loc: ['body','campo'], msg}]
    const list = this.details.errors
    if (Array.isArray(list)) {
      const out: FieldErrors = {}
      for (const item of list) {
        if (item && typeof item === 'object') {
          const it = item as { loc?: unknown[]; msg?: unknown }
          const loc = Array.isArray(it.loc) ? it.loc.filter((p) => p !== 'body' && p !== 'query').join('.') : ''
          if (loc && typeof it.msg === 'string' && !out[loc]) out[loc] = it.msg
        }
      }
      return out
    }
    const fields = this.details.fields
    if (fields && typeof fields === 'object' && !Array.isArray(fields)) {
      const out: FieldErrors = {}
      for (const [k, v] of Object.entries(fields as Record<string, unknown>)) {
        if (typeof v === 'string') out[k] = v
        else if (Array.isArray(v) && typeof v[0] === 'string') out[k] = v[0]
      }
      return out
    }
    return {}
  }

  get isUnauthorized() {
    return this.status === 401
  }
  get isForbidden() {
    return this.status === 403
  }
  get isNotFound() {
    return this.status === 404
  }
  get isNetwork() {
    return this.status === 0
  }
}

export function isApiError(e: unknown): e is ApiError {
  return e instanceof ApiError
}

const DEFAULT_MESSAGES: Record<number, string> = {
  0: 'Sem conexão com o servidor. Verifique sua internet e tente novamente.',
  400: 'Não foi possível concluir a operação. Revise os dados.',
  401: 'Sua sessão expirou. Entre novamente.',
  403: 'Você não tem permissão para esta ação.',
  404: 'Não encontrado ou fora do seu acesso.',
  409: 'Este registro foi alterado por outra pessoa ou a operação já foi feita. Atualize e tente de novo.',
  422: 'Alguns campos estão inválidos. Revise e tente novamente.',
  423: 'Recurso bloqueado temporariamente.',
  429: 'Muitas tentativas em pouco tempo. Aguarde e tente novamente.',
  500: 'Erro no servidor. Tente novamente em alguns instantes.',
}

export function defaultMessageFor(status: number): string {
  if (DEFAULT_MESSAGES[status]) return DEFAULT_MESSAGES[status]
  if (status >= 500) return DEFAULT_MESSAGES[500]
  return 'Ocorreu um erro inesperado. Tente novamente.'
}

/** Converte o corpo de erro (envelope v1 ou legado FastAPI) em ApiError. */
export function toApiError(
  status: number,
  body: unknown,
  opts: { requestId?: string; retryAfterHeader?: string | null } = {},
): ApiError {
  let code = status === 0 ? 'NETWORK_ERROR' : `HTTP_${status}`
  let message = defaultMessageFor(status)
  let details: Record<string, unknown> = {}

  if (body && typeof body === 'object') {
    const b = body as Record<string, unknown>
    const env = b.error
    if (env && typeof env === 'object') {
      const e = env as Record<string, unknown>
      if (typeof e.code === 'string') code = e.code
      if (typeof e.message === 'string' && e.message.trim()) message = e.message
      if (e.details && typeof e.details === 'object') details = { ...(e.details as Record<string, unknown>) }
    } else if ('detail' in b) {
      // Formato legado do FastAPI — mantido até o backend migrar ao envelope
      const d = b.detail
      if (typeof d === 'string' && d.trim()) message = d
      else if (Array.isArray(d)) {
        const fields: Record<string, string> = {}
        for (const item of d) {
          if (item && typeof item === 'object') {
            const it = item as { loc?: unknown[]; msg?: string }
            const loc = Array.isArray(it.loc) ? it.loc.filter((p) => p !== 'body').join('.') : ''
            if (loc && it.msg) fields[loc] = it.msg
          }
        }
        details = { fields }
      }
    }
  }

  if (opts.retryAfterHeader && details.retry_after_s === undefined) {
    const n = Number(opts.retryAfterHeader)
    if (Number.isFinite(n)) details.retry_after_s = n
  }

  return new ApiError({ status, code, message, details, requestId: opts.requestId })
}

/** Mensagem para exibir ao usuário a partir de qualquer erro. */
export function errorMessage(e: unknown): string {
  if (isApiError(e)) return e.message
  if (e instanceof Error && e.message) return e.message
  return defaultMessageFor(-1)
}
