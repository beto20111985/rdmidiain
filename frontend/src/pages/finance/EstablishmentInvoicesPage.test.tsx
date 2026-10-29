import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import EstablishmentInvoicesPage from './EstablishmentInvoicesPage'
import { useAuthStore } from '@/stores/authStore'
import { useContextStore } from '@/stores/contextStore'
import { makeMe, renderWithProviders } from '@/test/utils'
import { fetchRouter, page } from '@/components/finance/testing'
import { tokenStore } from '@/lib/http'

const CTX = [{ key: 'est:org1', label: 'Minha unidade', kind: 'ESTABLISHMENT' as const, org_id: 'org1' }]
const base = { org_id: 'org1', reference_type: 'partner_charge', reference_id: 'x', eligible_revenue_cents: 0, services_cents: 0, currency: 'BRL', method: 'PIX', installments: null, dispute_status: null, provider: 'cloudpaypag', provider_external_ref: null, provider_transaction_id: null, pix_end_to_end_id: null, paid_amount_cents: null, fee_cents: null, net_cents: null, refunded_cents: 0, provider_mode_fake: true }

beforeEach(() => {
  useContextStore.getState().reset()
  const me = makeMe({ permissions: ['withdrawals.view', 'establishments.view'], contexts: CTX })
  useAuthStore.setState({ status: 'authenticated', me, demo: false })
  useContextStore.getState().syncFromMe(CTX)
})
afterEach(() => {
  tokenStore.clear()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('Minha unidade → Financeiro → Faturas', () => {
  it('lista mensalidade pendente com PIX (QR local) e baixa o comprovante da fatura paga', async () => {
    const user = userEvent.setup()
    tokenStore.set('token-de-teste')
    const clicks: string[] = []
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      clicks.push(this.download)
    })
    URL.createObjectURL = vi.fn(() => 'blob:x')
    URL.revokeObjectURL = vi.fn()
    const r = fetchRouter([
      { match: /\/establishments\?/, reply: () => page([{ id: 'est1', org_id: 'org1', name: 'Mercado Teste', timezone: 'America/Sao_Paulo' }]) },
      {
        match: /\/financial\/establishments\/est1\/charges\?/,
        reply: () =>
          page([
            { ...base, id: 'o1', items: [{ kind: 'SERVICE', service_kind: 'MONTHLY_FEE', description: 'Mensalidade 10/2026', amount_cents: 9900 }], amount_cents: 9900, status: 'PENDING', pix_qr_code: '00020126580014BR.GOV.BCB.PIX0136teste', expires_at: '2026-10-10T12:00:00Z', paid_at: null, created_at: '2026-10-01T12:00:00Z' },
            { ...base, id: 'o2', items: [{ kind: 'SERVICE', service_kind: 'DEPOSIT', description: 'Caução TV', amount_cents: 50000 }], amount_cents: 50000, status: 'PAID', pix_qr_code: null, expires_at: null, paid_at: '2026-09-02T12:00:00Z', created_at: '2026-09-01T12:00:00Z' },
          ]),
      },
      {
        match: /\/charges\/o2\/receipt/,
        reply: () => new Response(new Blob(['%PDF']), { status: 200, headers: { 'content-type': 'application/pdf', 'content-disposition': 'attachment; filename="comprovante-o2.pdf"' } }),
      },
    ])
    renderWithProviders(<EstablishmentInvoicesPage />)
    expect(await screen.findByText('Mensalidade')).toBeInTheDocument()
    expect(screen.getByText('Caução (reembolsável)')).toBeInTheDocument()
    expect(screen.getByText('R$ 99,00')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /Baixar comprovante de R\$\s500,00/ }))
    await waitFor(() => expect(clicks).toEqual(['comprovante-o2.pdf']))
    expect(r.find('GET', /\/charges\/o2\/receipt/)[0].headers.Authorization).toBe('Bearer token-de-teste')

    await user.click(screen.getByRole('button', { name: /Pagar fatura de R\$\s99,00/ }))
    expect(await screen.findByRole('img', { name: /QR Code PIX de R\$\s99,00/ })).toBeInTheDocument()
    expect(screen.getByDisplayValue('00020126580014BR.GOV.BCB.PIX0136teste')).toBeInTheDocument()
  }, 20_000)
})
