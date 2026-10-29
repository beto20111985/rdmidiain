/**
 * Download/visualização de arquivos binários com a MESMA autenticação do cliente HTTP
 * (Bearer em memória, X-Request-ID, X-RD-Context, um refresh em 401) e o envelope de erro
 * do contrato v1. Usado para CSV da auditoria, arquivos de arte, anexos, logo e
 * pré-visualizações renderizadas no servidor (PNG/JPG). Nunca grava token em URL.
 */
import { buildUrl, getActiveContextHeader, newRequestId, refreshAccessToken, tokenStore, type QueryParams } from '@/lib/http'
import { toApiError } from '@/lib/errors'

export interface BinaryOptions {
  method?: 'GET' | 'POST'
  body?: unknown
  query?: QueryParams
  signal?: AbortSignal
}

export interface BinaryResult {
  blob: Blob
  filename: string | null
  contentType: string
}

async function sendOnce(path: string, opts: BinaryOptions): Promise<{ res: Response; requestId: string }> {
  const requestId = newRequestId()
  const headers: Record<string, string> = { 'X-Request-ID': requestId }
  const token = tokenStore.get()
  if (token) headers.Authorization = `Bearer ${token}`
  const ctx = getActiveContextHeader()
  if (ctx) headers['X-RD-Context'] = ctx
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json'
  try {
    const res = await fetch(buildUrl(path, opts.query), {
      method: opts.method ?? 'GET',
      headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      credentials: 'omit',
      signal: opts.signal,
    })
    return { res, requestId }
  } catch (err) {
    if ((err as { name?: string })?.name === 'AbortError') throw err
    throw toApiError(0, undefined, { requestId })
  }
}

/** Extrai o nome do arquivo do Content-Disposition (filename="..."). */
export function filenameFromDisposition(header: string | null): string | null {
  if (!header) return null
  const star = /filename\*=UTF-8''([^;]+)/i.exec(header)
  if (star) {
    try {
      return decodeURIComponent(star[1].trim())
    } catch {
      /* cai no formato simples */
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(header)
  return plain ? plain[1].trim() : null
}

export async function fetchBinary(path: string, opts: BinaryOptions = {}): Promise<BinaryResult> {
  let { res, requestId } = await sendOnce(path, opts)
  if (res.status === 401) {
    const renewed = await refreshAccessToken()
    if (renewed) ({ res, requestId } = await sendOnce(path, opts))
  }
  if (!res.ok) {
    let body: unknown
    try {
      body = await res.json()
    } catch {
      body = undefined
    }
    throw toApiError(res.status, body, {
      requestId: res.headers.get('x-request-id') ?? requestId,
      retryAfterHeader: res.headers.get('retry-after'),
    })
  }
  const blob = await res.blob()
  return {
    blob,
    filename: filenameFromDisposition(res.headers.get('content-disposition')),
    contentType: res.headers.get('content-type') ?? blob.type,
  }
}

/** Entrega o arquivo ao navegador (download). */
export function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.rel = 'noopener'
  document.body.appendChild(a)
  a.click()
  a.remove()
  // Revoga depois que o navegador iniciar o download.
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export async function downloadFile(path: string, fallbackName: string, opts: BinaryOptions = {}) {
  const r = await fetchBinary(path, opts)
  saveBlob(r.blob, r.filename ?? fallbackName)
  return r
}
