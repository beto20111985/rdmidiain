/**
 * Download autenticado de arquivos (PDF/XLSX/CSV) — "fetch com Bearer → arquivo".
 *
 * O token vive só em memória (nunca em URL/localStorage), então links diretos não servem:
 * buscamos o arquivo com `Authorization: Bearer`, renovamos UMA vez em 401 (mesmo
 * refresh single-flight do cliente HTTP) e entregamos ao navegador via Blob + <a download>.
 * Erros chegam como ApiError (envelope do contrato v1).
 */
import { buildUrl, getActiveContextHeader, newRequestId, refreshAccessToken, tokenStore, type QueryParams } from '@/lib/http'
import { toApiError } from '@/lib/errors'

export interface DownloadOptions {
  method?: 'GET' | 'POST'
  query?: QueryParams
  body?: unknown
  /** Nome usado quando o servidor não envia Content-Disposition. */
  fallbackFilename: string
  signal?: AbortSignal
}

export interface DownloadResult {
  filename: string
  size: number
  contentType: string
  /** SHA-256 informado pelo servidor (X-Content-SHA256), quando houver. */
  sha256: string | null
}

/** Extrai o nome do arquivo de `Content-Disposition` (filename* RFC 5987 ou filename). */
export function filenameFromDisposition(header: string | null, fallback: string): string {
  if (!header) return fallback
  const star = /filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)/.exec(header)
  if (star) {
    try {
      return sanitize(decodeURIComponent(star[1].trim().replace(/^"|"$/g, ''))) || fallback
    } catch {
      /* cai no filename simples */
    }
  }
  const plain = /filename\s*=\s*"?([^";]+)"?/.exec(header)
  return plain ? sanitize(plain[1].trim()) || fallback : fallback
}

function sanitize(name: string): string {
  // Sem caminhos nem caracteres de controle
  // eslint-disable-next-line no-control-regex
  return name.replace(/[\\/]/g, '_').replace(/[\u0000-\u001f]/g, '').slice(0, 180)
}

async function send(path: string, opts: DownloadOptions): Promise<Response> {
  const token = tokenStore.get()
  const headers: Record<string, string> = { Accept: '*/*', 'X-Request-ID': newRequestId() }
  if (token) headers.Authorization = `Bearer ${token}`
  const ctx = getActiveContextHeader()
  if (ctx) headers['X-RD-Context'] = ctx
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json'
  return fetch(buildUrl(path, opts.query), {
    method: opts.method ?? 'GET',
    headers,
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
    credentials: 'omit',
    signal: opts.signal,
  })
}

/** Busca o arquivo autenticado e devolve o Blob (sem salvar). */
export async function fetchFile(path: string, opts: DownloadOptions): Promise<{ blob: Blob } & DownloadResult> {
  let res: Response
  try {
    res = await send(path, opts)
    if (res.status === 401) {
      const renewed = await refreshAccessToken()
      if (renewed) res = await send(path, opts)
    }
  } catch (err) {
    if ((err as { name?: string })?.name === 'AbortError') throw err
    throw toApiError(0, undefined)
  }
  if (!res.ok) {
    let body: unknown
    try {
      body = (res.headers.get('content-type') ?? '').includes('json') ? await res.json() : undefined
    } catch {
      body = undefined
    }
    throw toApiError(res.status, body, { requestId: res.headers.get('x-request-id') ?? undefined })
  }
  const blob = await res.blob()
  return {
    blob,
    filename: filenameFromDisposition(res.headers.get('content-disposition'), opts.fallbackFilename),
    size: blob.size,
    contentType: res.headers.get('content-type') ?? blob.type,
    sha256: res.headers.get('x-content-sha256'),
  }
}

/** Entrega um Blob ao navegador como download. */
export function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.rel = 'noopener'
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  a.remove()
  // Revoga depois que o navegador iniciou o download
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

/** Download autenticado completo: busca + salva. */
export async function downloadFile(path: string, opts: DownloadOptions): Promise<DownloadResult> {
  const { blob, ...meta } = await fetchFile(path, opts)
  saveBlob(blob, meta.filename)
  return meta
}
