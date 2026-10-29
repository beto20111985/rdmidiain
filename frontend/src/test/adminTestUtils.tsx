/**
 * Utilidades de TESTE das páginas de Administração/Suporte/Design/Notificações (FE-ADM).
 * Mocks de API são dados de teste — nunca usados fora dos testes.
 */
import * as React from 'react'
import { Route, Routes } from 'react-router-dom'
import { vi } from 'vitest'
import { useAuthStore } from '@/stores/authStore'
import { useContextStore } from '@/stores/contextStore'
import type { AppContextInfo, Assignment, Me } from '@/features/auth/types'
import { PageMetaProvider, usePageMetaValue } from '@/components/layout/page-meta'
import { jsonResponse, makeMe, mockFetch, renderWithProviders } from './utils'

export const RD_CONTEXT: AppContextInfo = { key: 'rd', label: 'RD Mídia Indoor', kind: 'PLATFORM' }
export const EST_CONTEXT: AppContextInfo = { key: 'est:o1', label: 'Minha unidade — Mercado Teste', kind: 'ESTABLISHMENT', org_id: 'o1' }
export const ADV_CONTEXT: AppContextInfo = { key: 'adv:o2', label: 'Minhas campanhas — Loja Teste', kind: 'ADVERTISER', org_id: 'o2' }

export function signInAs(opts: {
  permissions: string[]
  contexts?: AppContextInfo[]
  assignments?: Assignment[]
  user?: Partial<Me['user']>
  impersonation?: Me['impersonation']
}): Me {
  const contexts = opts.contexts ?? [RD_CONTEXT]
  const base = makeMe()
  const me = makeMe({
    permissions: opts.permissions,
    contexts,
    assignments: opts.assignments ?? [{ role: 'admin', scope_type: 'GLOBAL' }],
    user: { ...base.user, ...opts.user },
    impersonation: opts.impersonation ?? { active: false, real_user: null },
  })
  useAuthStore.setState({ status: 'authenticated', me, demo: false, notice: null })
  useContextStore.getState().syncFromMe(contexts)
  return me
}

export function resetSession() {
  useContextStore.getState().reset()
  useAuthStore.setState({ status: 'anonymous', me: null, demo: false, notice: null })
}

export interface Call {
  method: string
  path: string
  url: string
  body: unknown
  headers: Record<string, string>
}

type Handler = (call: Call) => Response | Promise<Response> | undefined

/**
 * Fetch falso por rota: devolve 404 (envelope v1) para o que não foi tratado e registra
 * todas as chamadas (método, caminho sem /api/v1, corpo JSON, cabeçalhos).
 */
export function mockApi(handler: Handler) {
  const calls: Call[] = []
  const fn = mockFetch(async (url, init) => {
    const u = new URL(url, 'http://localhost')
    let body: unknown = undefined
    if (typeof init.body === 'string') {
      try {
        body = JSON.parse(init.body)
      } catch {
        body = init.body
      }
    } else if (init.body) body = init.body
    const call: Call = {
      method: (init.method ?? 'GET').toUpperCase(),
      path: u.pathname.replace(/^\/api\/v1/, '') + (u.search || ''),
      url,
      body,
      headers: (init.headers ?? {}) as Record<string, string>,
    }
    calls.push(call)
    const res = await handler(call)
    return res ?? jsonResponse({ error: { code: 'NOT_FOUND', message: 'Não encontrado.' } }, 404)
  })
  return { fn, calls }
}

export function apiError(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
  return jsonResponse({ error: { code, message, details } }, status)
}

/** Cabeçalho mínimo de teste: o único <h1> e as ações declaradas pela página (PageHeader). */
function TestHeader() {
  const meta = usePageMetaValue()
  return (
    <header>
      <h1>{meta.title}</h1>
      {meta.description ? <div data-testid="page-description">{meta.description}</div> : null}
      {meta.actions ? <div data-testid="page-actions">{meta.actions}</div> : null}
    </header>
  )
}

export function withPageMeta(ui: React.ReactElement) {
  return (
    <PageMetaProvider>
      <TestHeader />
      {ui}
    </PageMetaProvider>
  )
}

/** Renderiza a página dentro de uma rota com parâmetros (useParams) e o cabeçalho de teste. */
export function renderRoute(element: React.ReactElement, pattern: string, url: string) {
  return renderWithProviders(
    withPageMeta(
      <Routes>
        <Route path={pattern} element={element} />
        <Route path="*" element={<p>fora da rota de teste</p>} />
      </Routes>,
    ),
    { route: url },
  )
}

/** jsdom não implementa object URLs. */
export function stubObjectUrls() {
  const create = vi.fn(() => 'blob:teste')
  const revoke = vi.fn()
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, writable: true, value: create })
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, writable: true, value: revoke })
  return { create, revoke }
}

export { jsonResponse }
