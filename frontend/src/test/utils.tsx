import * as React from 'react'
import { render, type RenderOptions } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { vi } from 'vitest'
import { TooltipProvider } from '@/components/ui/tooltip'
import type { Me } from '@/features/auth/types'

export function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', ...headers },
  })
}

export type FetchHandler = (url: string, init: RequestInit) => Response | Promise<Response>

/** Substitui o fetch global; devolve o mock para inspecionar chamadas. */
export function mockFetch(handler: FetchHandler) {
  const fn = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => handler(String(input), init))
  vi.stubGlobal('fetch', fn)
  return fn
}

export function headersOf(init: RequestInit): Record<string, string> {
  return (init.headers ?? {}) as Record<string, string>
}

export function makeMe(partial: Partial<Me> = {}): Me {
  return {
    user: { id: 'u1', name: 'Pessoa Teste', email: 'teste@exemplo.invalid', mfa_enabled: true, is_admin_master: false, theme: null },
    permissions: [],
    assignments: [],
    contexts: [{ key: 'rd', label: 'RD Mídia Indoor', kind: 'PLATFORM' }],
    impersonation: { active: false, real_user: null },
    mfa: { required: false, configured: true },
    ...partial,
  }
}

export function renderWithProviders(
  ui: React.ReactElement,
  { route = '/', queryClient, ...options }: { route?: string; queryClient?: QueryClient } & Omit<RenderOptions, 'wrapper'> = {},
) {
  const qc = queryClient ?? new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={[route]}>{children}</MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>
  )
  return { queryClient: qc, ...render(ui, { wrapper: Wrapper, ...options }) }
}
