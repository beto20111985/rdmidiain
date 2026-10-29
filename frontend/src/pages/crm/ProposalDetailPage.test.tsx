import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router-dom'
import ProposalDetailPage from './ProposalDetailPage'
import { useAuthStore } from '@/stores/authStore'
import { useContextStore } from '@/stores/contextStore'
import { makeMe, renderWithProviders } from '@/test/utils'
import { tokenStore } from '@/lib/http'
import { fetchRouter, page } from '@/components/finance/testing'
import type { Proposal } from './api'
import { PageMetaProvider, usePageMetaValue } from '@/components/layout/page-meta'

/** Renderiza as ações do cabeçalho (no app, o Header da fundação faz isso). */
function HeaderActions() {
  const meta = usePageMetaValue()
  return <div data-testid="header-actions">{meta.actions}</div>
}

const SELLER = '11111111-0000-4000-8000-000000000001'
const MANAGER = '22222222-0000-4000-8000-000000000002'

const proposal: Proposal = {
  id: 'p1',
  number: 'PROP-2026-000123',
  title: 'Lançamento — 20 telas',
  status: 'AGUARDANDO_APROVACAO',
  stored_status: 'AGUARDANDO_APROVACAO',
  lead_id: null,
  org_id: 'org9',
  seller_id: SELLER,
  city_key: null,
  subtotal_cents: 1000000,
  discount_bps: 1500,
  discount_cents: 150000,
  total_cents: 850000,
  discount_limit_bps: 1000,
  discount_approval_required: true,
  discount_justification: 'Cliente estratégico',
  discount_approved_by: null,
  discount_approved_at: null,
  discount_decision_reason: null,
  validity_days: 30,
  valid_until: null,
  expires_at: null,
  sent_at: null,
  accepted_at: null,
  converted_at: null,
  order_id: null,
  pdf_sha256: null,
  notes: null,
  version: 2,
  created_at: '2026-10-07T12:00:00Z',
  updated_at: '2026-10-07T12:00:00Z',
  items: [{ id: 'i1', position: 0, package_code: 'PCT-20', description: 'Pacote 20 telas', quantity: 1, unit_price_cents: 1000000, line_total_cents: 1000000 }],
}

function signIn(userId: string, permissions: string[]) {
  const me = makeMe({ permissions, user: { id: userId, name: 'Pessoa', email: 'p@rd.invalid', mfa_enabled: true, is_admin_master: false, theme: null } })
  useAuthStore.setState({ status: 'authenticated', me, demo: false })
  useContextStore.getState().syncFromMe(me.contexts)
}

function render() {
  return renderWithProviders(
    <PageMetaProvider>
      <HeaderActions />
      <Routes>
        <Route path="/crm/propostas/:id" element={<ProposalDetailPage />} />
      </Routes>
    </PageMetaProvider>,
    { route: '/crm/propostas/p1' },
  )
}

beforeEach(() => useContextStore.getState().reset())
afterEach(() => vi.unstubAllGlobals())

describe('Proposta com desconto acima do limite', () => {
  it('o VENDEDOR não aprova o próprio desconto (mesmo com crm.discount_approve)', async () => {
    signIn(SELLER, ['crm.view', 'crm.manage', 'crm.discount_approve', 'organizations.view'])
    fetchRouter([
      { match: /\/crm\/proposals\/p1$/, reply: () => proposal },
      { match: /\/organizations\?/, reply: () => page([{ id: 'org9', name: 'Cliente Nove' }]) },
    ])
    render()
    expect(await screen.findByText(/o vendedor não aprova o próprio desconto/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Aprovar desconto' })).not.toBeInTheDocument()
    // valores em BRL a partir de centavos; desconto em %
    expect(screen.getAllByText('R$ 8.500,00').length).toBeGreaterThan(0)
    expect(screen.getAllByText(/15,00%/).length).toBeGreaterThan(0)
  })

  it('um gestor com crm.discount_approve decide (recusa exige motivo)', async () => {
    const user = userEvent.setup()
    signIn(MANAGER, ['crm.view', 'crm.manage', 'crm.discount_approve'])
    const r = fetchRouter([
      { match: /\/crm\/proposals\/p1$/, reply: () => proposal },
      { method: 'POST', match: /\/crm\/proposals\/p1\/discount-decision/, reply: () => ({ ...proposal, status: 'APROVADA', stored_status: 'APROVADA' }) },
    ])
    render()
    await user.click(await screen.findByRole('button', { name: 'Recusar' }))
    expect(await screen.findByText('Informe o motivo da recusa.')).toBeInTheDocument()
    expect(r.find('POST', /discount-decision/)).toHaveLength(0)
    await user.click(screen.getByRole('button', { name: 'Aprovar desconto' }))
    await waitFor(() => expect(r.find('POST', /discount-decision/)).toHaveLength(1))
    expect(r.find('POST', /discount-decision/)[0].body).toEqual({ approve: true })
  }, 20_000)

  it('PDF da proposta é baixado com Bearer (nome do arquivo do servidor)', async () => {
    const user = userEvent.setup()
    signIn(MANAGER, ['crm.view'])
    const clicks: string[] = []
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      clicks.push(this.download)
    })
    URL.createObjectURL = vi.fn(() => 'blob:x')
    URL.revokeObjectURL = vi.fn()
    tokenStore.set('token-de-teste')
    const r = fetchRouter([
      { match: /\/crm\/proposals\/p1$/, reply: () => proposal },
      {
        match: /\/crm\/proposals\/p1\/pdf/,
        reply: () => new Response(new Blob(['%PDF']), { status: 200, headers: { 'content-type': 'application/pdf', 'content-disposition': 'attachment; filename="proposta-PROP-2026-000123.pdf"' } }),
      },
    ])
    render()
    await user.click(await screen.findByRole('button', { name: /PDF/ }))
    await waitFor(() => expect(clicks).toEqual(['proposta-PROP-2026-000123.pdf']))
    expect(r.find('GET', /\/pdf/)[0].headers.Authorization).toBe('Bearer token-de-teste')
    tokenStore.clear()
    vi.restoreAllMocks()
  })
})

describe('Conversão proposta → pedido (campanhas elegíveis do servidor)', () => {
  const accepted: Proposal = { ...proposal, status: 'ACEITA', stored_status: 'ACEITA', discount_approved_by: MANAGER, discount_approved_by_name: 'Gestora Comercial', discount_approved_at: '2026-10-07T13:00:00Z', accepted_at: '2026-10-07T14:00:00Z', seller_name: 'Vendedor Um', org_name: 'Cliente Nove' }

  it('vendedor sem campaigns.view escolhe entre as campanhas elegíveis; pendências bloqueiam; erro de CNPJ = dependência externa', async () => {
    const user = userEvent.setup()
    signIn(SELLER, ['crm.view', 'crm.manage'])
    const r = fetchRouter([
      { match: /\/crm\/proposals\/p1$/, reply: () => accepted },
      {
        match: /\/crm\/proposals\/p1\/convertible-campaigns/,
        reply: () => ({
          items: [
            { id: 'c-ok', name: 'Campanha Pronta', status: 'RASCUNHO', start_date: '2026-11-01', end_date: '2026-11-30', duration_s: 30, ready: true, issues: [] },
            { id: 'c-bad', name: 'Campanha Sem Datas', status: 'RASCUNHO', start_date: null, end_date: null, duration_s: 30, ready: false, issues: [{ code: 'SEM_DATAS', message: 'Defina o período da campanha.' }] },
          ],
        }),
      },
      {
        method: 'POST',
        match: /\/crm\/proposals\/p1\/convert/,
        reply: () =>
          new Response(
            JSON.stringify({ error: { code: 'PAYER_DOCUMENT_NOT_SUPPORTED_BY_GATEWAY', message: 'O meio de pagamento (CloudPayPag) ainda não aceita CNPJ alfanumérico.', details: { status: 'BLOQUEADO POR DEPENDÊNCIA EXTERNA' } } }),
            { status: 422, headers: { 'content-type': 'application/json' } },
          ),
      },
    ])
    render()
    // nomes resolvidos pelo servidor
    expect(await screen.findByText('Vendedor Um (você)')).toBeInTheDocument()
    expect(screen.getByText('Cliente Nove')).toBeInTheDocument()
    expect(screen.getByText(/Gestora Comercial/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Converter em pedido' }))
    expect(await screen.findByText('Defina o período da campanha.')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: /Campanha Sem Datas/ })).toBeDisabled()
    expect(r.find('GET', /\/campaigns\?/)).toHaveLength(0) // não usa campaigns.view
    await user.click(screen.getByRole('checkbox', { name: /Campanha Pronta/ }))
    await user.click(screen.getByRole('button', { name: 'Converter e gerar cobrança' }))
    await waitFor(() => expect(r.find('POST', /\/convert/)).toHaveLength(1))
    const call = r.find('POST', /\/convert/)[0]
    expect(call.body).toEqual({ campaign_ids: ['c-ok'] })
    expect(call.headers['Idempotency-Key']).toMatch(/^[0-9a-f-]{36}$/)
    expect(await screen.findByText('BLOQUEADO POR DEPENDÊNCIA EXTERNA')).toBeInTheDocument()
    expect(screen.getByText(/ainda não aceita CNPJ alfanumérico/)).toBeInTheDocument()
  }, 20_000)
})
