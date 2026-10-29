/**
 * APOIO DE TESTE (Vitest) das páginas FE-FIN — não é importado por código de produção.
 * Roteia `fetch` por método + regex de URL e registra as chamadas (corpo e cabeçalhos).
 * Os dados devolvidos são MOCKS DE TESTE, nunca dados de produção.
 */
import { vi } from 'vitest'
import { jsonResponse } from '@/test/utils'

export interface FetchCall {
  url: string
  method: string
  body: unknown
  headers: Record<string, string>
}

export interface FetchRoute {
  method?: string
  match: RegExp
  reply: (call: FetchCall) => Response | unknown
}

export function fetchRouter(routes: FetchRoute[]) {
  const calls: FetchCall[] = []
  const fn = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input)
    const method = (init.method ?? 'GET').toUpperCase()
    let body: unknown = init.body
    if (typeof init.body === 'string') {
      try {
        body = JSON.parse(init.body)
      } catch {
        body = init.body
      }
    }
    const call: FetchCall = { url, method, body, headers: (init.headers ?? {}) as Record<string, string> }
    calls.push(call)
    for (const r of routes) {
      if ((r.method ?? 'GET').toUpperCase() === method && r.match.test(url)) {
        const out = r.reply(call)
        return out instanceof Response ? out : jsonResponse(out)
      }
    }
    return jsonResponse({ error: { code: 'NOT_MOCKED', message: `não simulado no teste: ${method} ${url}` } }, 404)
  })
  vi.stubGlobal('fetch', fn)
  return { fn, calls, find: (method: string, re: RegExp) => calls.filter((c) => c.method === method && re.test(c.url)) }
}

export const page = <T,>(items: T[]) => ({ items, total: items.length, page: 1, page_size: Math.max(items.length, 25) })
