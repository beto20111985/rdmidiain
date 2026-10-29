/**
 * Upload com PROGRESSO REAL (bytes enviados), via XMLHttpRequest — `fetch` não expõe
 * progresso de envio. Mesmos cabeçalhos do cliente HTTP (Bearer em memória, X-Request-ID,
 * X-RD-Context) e mesmo envelope de erro. Em 401 tenta um refresh e repete uma vez.
 * O estado "processando" (transcodificação) chega depois pelo evento WS `media.processing`.
 */
import { buildUrl, getActiveContextHeader, newRequestId, refreshAccessToken, tokenStore } from './http'
import { ApiError, toApiError } from './errors'

export interface UploadOptions {
  path: string
  form: FormData
  onProgress?: (fraction: number) => void
  signal?: AbortSignal
  idempotencyKey?: string
}

function send<T>({ path, form, onProgress, signal, idempotencyKey }: UploadOptions): Promise<{ status: number; body: unknown; requestId: string; data?: T }> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    const requestId = newRequestId()
    xhr.open('POST', buildUrl(path))
    xhr.withCredentials = false
    xhr.setRequestHeader('Accept', 'application/json')
    xhr.setRequestHeader('X-Request-ID', requestId)
    const token = tokenStore.get()
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)
    const ctx = getActiveContextHeader()
    if (ctx) xhr.setRequestHeader('X-RD-Context', ctx)
    if (idempotencyKey) xhr.setRequestHeader('Idempotency-Key', idempotencyKey)
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total)
    }
    xhr.onload = () => {
      let body: unknown
      try {
        body = xhr.responseText ? JSON.parse(xhr.responseText) : undefined
      } catch {
        body = xhr.responseText
      }
      resolve({ status: xhr.status, body, requestId })
    }
    xhr.onerror = () => reject(toApiError(0, undefined, { requestId }))
    xhr.onabort = () => reject(new DOMException('Envio cancelado.', 'AbortError'))
    signal?.addEventListener('abort', () => xhr.abort(), { once: true })
    xhr.send(form)
  })
}

export async function uploadWithProgress<T = unknown>(opts: UploadOptions): Promise<T> {
  let res = await send<T>(opts)
  if (res.status === 401) {
    const t = await refreshAccessToken()
    if (t) res = await send<T>(opts)
  }
  if (res.status < 200 || res.status >= 300) {
    throw toApiError(res.status, res.body, { requestId: res.requestId })
  }
  return res.body as T
}

export { ApiError }
