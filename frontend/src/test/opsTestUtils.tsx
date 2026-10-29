/**
 * Utilidades de TESTE das páginas de Operação (mocks de fetch por rota — dados fictícios só de teste).
 */
import type { ReactElement } from 'react'
import { Route, Routes } from 'react-router-dom'
import { vi } from 'vitest'
import { jsonResponse, makeMe, mockFetch, renderWithProviders } from '@/test/utils'
import { useAuthStore } from '@/stores/authStore'
import { tokenStore } from '@/lib/http'
import type { Permission } from '@/lib/permissions'
import { PageMetaProvider, usePageMetaValue } from '@/components/layout/page-meta'

export function setMe(permissions: Permission[]) {
  tokenStore.set('token-de-teste')
  useAuthStore.setState({ status: 'authenticated', me: makeMe({ permissions }), demo: false })
}

type Handler = (url: URL, init: RequestInit) => Response | Promise<Response> | unknown
export interface MockRoute {
  method?: string
  path: string | RegExp
  reply: Handler
}

/** fetch falso por método + caminho (sem query). Sem rota → 404 no envelope do contrato. */
export function routeFetch(routes: MockRoute[]) {
  return mockFetch(async (raw, init) => {
    const url = new URL(raw, 'http://localhost')
    const method = (init.method ?? 'GET').toUpperCase()
    for (const r of routes) {
      if ((r.method ?? 'GET').toUpperCase() !== method) continue
      const ok = typeof r.path === 'string' ? url.pathname === `/api/v1${r.path}` : r.path.test(url.pathname)
      if (!ok) continue
      const out = await r.reply(url, init)
      return out instanceof Response ? out : jsonResponse(out)
    }
    return jsonResponse({ error: { code: 'NOT_FOUND', message: 'Não encontrado.' } }, 404)
  })
}

export function apiError(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
  return jsonResponse({ error: { code, message, details } }, status)
}

/** Emula o Header do AppShell: único <h1> + ações da página (PageHeader). */
function HeaderStub() {
  const meta = usePageMetaValue()
  return (
    <header>
      {meta.title ? <h1>{meta.title}</h1> : null}
      <div data-testid="page-actions">{meta.actions}</div>
    </header>
  )
}

export function renderAt(element: ReactElement, path: string, route: string) {
  return renderWithProviders(
    <PageMetaProvider>
      <HeaderStub />
      <Routes>
        <Route path={path} element={element} />
        <Route path="*" element={<p>outra página</p>} />
      </Routes>
    </PageMetaProvider>,
    { route },
  )
}

export function callsTo(fetchMock: ReturnType<typeof vi.fn>, pathPart: string, method = 'GET') {
  return fetchMock.mock.calls.filter(
    ([u, init]) => String(u).includes(pathPart) && ((init as RequestInit | undefined)?.method ?? 'GET').toUpperCase() === method,
  )
}
