import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ReportsPage from './ReportsPage'
import { useAuthStore } from '@/stores/authStore'
import { useContextStore } from '@/stores/contextStore'
import type { AppContextInfo } from '@/features/auth/types'
import { makeMe, renderWithProviders } from '@/test/utils'
import { fetchRouter, page } from '@/components/finance/testing'
import { tokenStore } from '@/lib/http'

function signIn(permissions: string[], contexts: AppContextInfo[]) {
  const me = makeMe({ permissions, contexts })
  useAuthStore.setState({ status: 'authenticated', me, demo: false })
  useContextStore.getState().syncFromMe(contexts)
}

const DATA = {
  title: 'Exibições por dia',
  audience: 'EQUIPE',
  columns: [
    { key: 'day', label: 'Dia', kind: 'date' },
    { key: 'valid', label: 'Exibições válidas', kind: 'int' },
  ],
  rows: [
    { day: '2026-10-06', valid: 1200 },
    { day: '2026-10-07', valid: 34 },
  ],
  meta: [{ label: 'Público', value: 'Equipe RD' }],
  notes: ['Exibição = reprodução completa confirmada pelo player; não é pessoa impactada.'],
  total: 2,
}

let clicks: string[] = []
beforeEach(() => {
  useContextStore.getState().reset()
  clicks = []
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    clicks.push(this.download)
  })
  URL.createObjectURL = vi.fn(() => 'blob:x')
  URL.revokeObjectURL = vi.fn()
})
afterEach(() => {
  tokenStore.clear()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('Relatórios', () => {
  it('equipe RD: deixa claro "exibições válidas comprovadas" e exporta PDF com download autenticado', async () => {
    const user = userEvent.setup()
    tokenStore.set('token-de-teste')
    signIn(['reports.view', 'reports.export', 'campaigns.view', 'screens.view'], makeMe().contexts)
    const r = fetchRouter([
      { match: /\/reports\/catalog/, reply: () => ({ audience: 'EQUIPE', items: [{ key: 'exibicoes_por_dia', label: 'Exibições por dia' }, { key: 'falhas', label: 'Falhas de exibição' }] }) },
      { match: /\/reports\/data\/exibicoes_por_dia/, reply: () => DATA },
      { match: /\/reports\/exports\?/, reply: () => page([]) },
      { match: /\/reports\/proofs/, reply: () => page([]) },
      { match: /\/campaigns\?/, reply: () => page([{ id: 'c1', name: 'Campanha Teste' }]) },
      { match: /\/screens\?/, reply: () => page([{ id: 's1', name: 'Caixa 1', friendly_id: 'TEL-GO-7Q3M' }]) },
      {
        method: 'POST',
        match: /\/reports\/exports$/,
        reply: (c) => ({ id: 'e1', report_key: (c.body as { report_key: string }).report_key, format: 'pdf', status: 'CONCLUIDO', filename: 'exibicoes_por_dia_2026-09-08_2026-10-07.pdf', size_bytes: 2048, sha256: 'a', row_count: 2, emailed_to: null, params: {}, created_at: '2026-10-07T20:00:00Z', expires_at: null, download_url: '/api/v1/reports/exports/e1/download' }),
      },
      {
        match: /\/reports\/exports\/e1\/download/,
        reply: () => new Response(new Blob(['%PDF']), { status: 200, headers: { 'content-type': 'application/pdf', 'content-disposition': 'attachment; filename="exibicoes_por_dia_2026-09-08_2026-10-07.pdf"' } }),
      },
    ])
    renderWithProviders(<ReportsPage />)
    expect(await screen.findByText('Exibições válidas comprovadas')).toBeInTheDocument()
    expect(screen.getByText(/não a programação planejada/)).toBeInTheDocument()
    expect(await screen.findByText('1.200')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Comprovantes de veiculação' })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Exportar em PDF' }))
    await waitFor(() => expect(clicks).toEqual(['exibicoes_por_dia_2026-09-08_2026-10-07.pdf']))
    const post = r.find('POST', /\/reports\/exports$/)[0]
    expect(post.body).toMatchObject({ report_key: 'exibicoes_por_dia', format: 'pdf', audience: 'EQUIPE', email_to_me: false })
    expect(r.find('GET', /\/exports\/e1\/download/)[0].headers.Authorization).toBe('Bearer token-de-teste')
  }, 20_000)

  it('equipe sem reports.export não vê botões de exportação', async () => {
    signIn(['reports.view'], makeMe().contexts)
    fetchRouter([
      { match: /\/reports\/catalog/, reply: () => ({ audience: 'EQUIPE', items: [{ key: 'exibicoes_por_dia', label: 'Exibições por dia' }] }) },
      { match: /\/reports\/data\//, reply: () => DATA },
      { match: /\/reports\/exports\?/, reply: () => page([]) },
      { match: /\/reports\/proofs/, reply: () => page([]) },
    ])
    renderWithProviders(<ReportsPage />)
    expect(await screen.findByText(/Exportar exige a permissão reports.export/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Exportar em/ })).not.toBeInTheDocument()
  })

  it('estabelecimento: público decidido pelo servidor, sem filtro de campanha e sem identificar anunciantes', async () => {
    const ctx: AppContextInfo[] = [{ key: 'est:org1', label: 'Minha unidade', kind: 'ESTABLISHMENT', org_id: 'org1' }]
    signIn(['reports.view', 'screens.view'], ctx)
    const r = fetchRouter([
      { match: /\/reports\/catalog/, reply: () => ({ audience: 'ESTABELECIMENTO', items: [{ key: 'exibicoes_por_tipo', label: 'Exibições por tipo de conteúdo' }] }) },
      { match: /\/reports\/data\//, reply: () => ({ ...DATA, title: 'Exibições por tipo de conteúdo', audience: 'ESTABELECIMENTO', columns: [{ key: 'kind', label: 'Tipo de conteúdo', kind: 'text' }, { key: 'valid', label: 'Exibições válidas', kind: 'int' }], rows: [{ kind: 'Externo (anúncios)', valid: 10 }] }) },
      { match: /\/reports\/exports\?/, reply: () => page([]) },
      { match: /\/screens\?/, reply: () => page([]) },
    ])
    renderWithProviders(<ReportsPage />)
    expect(await screen.findByText(/anunciantes e campanhas não são identificados/)).toBeInTheDocument()
    expect(screen.queryByText('Campanha', { selector: 'label' })).not.toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: /Comprovantes/ })).not.toBeInTheDocument()
    await waitFor(() => expect(r.find('GET', /\/reports\/data\//).length).toBeGreaterThan(0))
    expect(r.find('GET', /\/reports\/data\//)[0].url).toMatch(/audience=ESTABELECIMENTO/)
    expect(r.find('GET', /\/reports\/data\//)[0].url).toMatch(/org_id=org1/)
  })
})
