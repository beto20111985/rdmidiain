import { beforeEach, describe, expect, it } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { PromotionsTab } from '../pricing/PromotionsTab'
import { OverridesTab, RulesTab } from '../taxonomy/MatrixTabs'
import { CheckoutError } from '../checkout/shared'
import CampaignsListPage from '../CampaignsListPage'
import { toApiError } from '@/lib/errors'
import { jsonResponse, mockFetch, renderWithProviders } from '@/test/utils'
import { campaignFixture, sentBodies, signInAs } from './fixtures'

// Funções puras de data/fuso: testadas na fundação (src/lib/dates.test.ts).

describe('telas que gravam datas', () => {
  beforeEach(() => signInAs('staff'))

  it('promoção (sem estabelecimento) usa America/Sao_Paulo', async () => {
    const fetchMock = mockFetch((url, init) => {
      if (url.endsWith('/pricing/promotions') && init.method === 'POST') return jsonResponse({ id: 'p1' }, 201)
      return jsonResponse({ items: [] })
    })
    const user = userEvent.setup()
    renderWithProviders(<PromotionsTab canManage />)
    await user.click(await screen.findByRole('button', { name: 'Nova promoção' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByRole('textbox', { name: /Nome/ }), 'Primavera')
    await user.type(within(dialog).getByRole('textbox', { name: /Desconto/ }), '10')
    const dates = dialog.querySelectorAll('input[type="date"]')
    fireEvent.change(dates[0], { target: { value: '2026-10-07' } })
    fireEvent.change(dates[1], { target: { value: '2026-10-31' } })
    await user.click(within(dialog).getByRole('button', { name: 'Criar promoção' }))
    await waitFor(() => expect(sentBodies(fetchMock).some((b) => b.name === 'Primavera')).toBe(true))
    const body = sentBodies(fetchMock).find((b) => b.name === 'Primavera')!
    expect(body.valid_from).toBe('2026-10-07T03:00:00Z')
    expect(body.valid_until).toBe('2026-11-01T02:59:59Z')
  }, 20000)

  it('lista de regras: vigência de regra de estabelecimento é EXIBIDA no fuso dele (Manaus), demais em Brasília', async () => {
    const rule = (id: string, subject_type: string, subject_id: string, valid_until: string) => ({
      id,
      subject_type,
      subject_id,
      object_type: 'AD_CATEGORY',
      object_id: 'n1',
      include_descendants: true,
      effect: 'ALLOW',
      reason: `regra ${id}`,
      valid_from: null,
      valid_until,
      active: true,
      version: 1,
      created_at: '2026-10-08T12:00:00Z',
      updated_at: null,
    })
    mockFetch((url) => {
      if (url.includes('/conflicts/rules'))
        return jsonResponse({
          items: [rule('r1', 'ESTABLISHMENT', 'e1', '2027-02-01T03:59:59Z'), rule('r2', 'EST_CATEGORY', 'n2', '2027-02-01T02:59:59Z')],
          total: 2,
          page: 1,
          page_size: 25,
        })
      if (url.endsWith('/establishments/e1')) return jsonResponse({ id: 'e1', name: 'Mercado Manaus', timezone: 'America/Manaus' })
      return jsonResponse({ items: [] })
    })
    renderWithProviders(<RulesTab />)
    expect(await screen.findByText('31/01/2027, 23:59 (horário de Manaus)', { exact: false })).toBeInTheDocument()
    // regra sem estabelecimento: mesmo instante "fim do dia 31/01" no horário de Brasília
    const r2 = (await screen.findByText('regra r2')).closest('tr')!
    expect(r2).toHaveTextContent('sem início → 31/01/2027, 23:59')
    expect(r2).not.toHaveTextContent('horário de')
  })

  it('override de estabelecimento em Manaus expira às 23:59:59 de Manaus', async () => {
    const fetchMock = mockFetch((url, init) => {
      if (url.endsWith('/conflicts/overrides') && init.method === 'POST') return jsonResponse({ id: 'o1', campaign_id: 'c1', establishment_id: 'e1', justification: 'x', approved_by: 'u', approved_at: '2026-10-07T12:00:00Z', expires_at: null, revoked_at: null, revoke_reason: null }, 201)
      if (url.includes('/conflicts/overrides')) return jsonResponse({ items: [] })
      if (url.includes('/campaigns?')) return jsonResponse({ items: [{ id: 'c1', name: 'Campanha X', status: 'ATIVA' }], total: 1, page: 1, page_size: 20 })
      if (url.includes('/establishments?'))
        return jsonResponse({ items: [{ id: 'e1', name: 'Mercado Manaus', city: 'Manaus', state: 'AM', timezone: 'America/Manaus' }], total: 1, page: 1, page_size: 20 })
      return jsonResponse({})
    })
    const user = userEvent.setup()
    const { container } = renderWithProviders(<OverridesTab canOverride />)
    await user.click(await screen.findByRole('button', { name: /Campanha X/ }))
    await user.click(await screen.findByRole('button', { name: /Mercado Manaus/ }))
    expect(screen.getByText(/fuso do estabelecimento \(America\/Manaus\)/)).toBeInTheDocument()
    fireEvent.change(screen.getByRole('textbox', { name: /Justificativa/ }), { target: { value: 'Contrato específico aprovado pela diretoria' } })
    fireEvent.change(container.querySelector('input[type="date"]') as HTMLInputElement, { target: { value: '2026-10-07' } })
    await user.click(screen.getByRole('button', { name: 'Registrar override' }))
    await waitFor(() => expect(sentBodies(fetchMock).some((b) => 'justification' in b)).toBe(true))
    expect(sentBodies(fetchMock).find((b) => 'justification' in b)!.expires_at).toBe('2026-10-08T03:59:59Z')
  }, 20000)
})

describe('pontos de integração anunciados pelo coordenador', () => {
  beforeEach(() => signInAs('advertiser'))

  it('lista usa display_status: REPRODUZINDO só quando o servidor diz; is_reproducing null ou falso → sem badge', async () => {
    mockFetch(() =>
      jsonResponse({
        items: [
          { ...campaignFixture({ id: 'a', name: 'Com PoP', status: 'ATIVA' }), is_reproducing: true, display_status: 'REPRODUZINDO' },
          { ...campaignFixture({ id: 'b', name: 'Ativa sem PoP', status: 'ATIVA' }), is_reproducing: false, display_status: 'ATIVA' },
          { ...campaignFixture({ id: 'c', name: 'Playback indisponível', status: 'ATIVA' }), is_reproducing: null, display_status: 'ATIVA' },
        ],
        total: 3,
        page: 1,
        page_size: 25,
      }),
    )
    renderWithProviders(<CampaignsListPage />, { route: '/anunciante/campanhas' })
    expect(await screen.findByText('Com PoP')).toBeInTheDocument()
    expect(screen.getAllByTestId('reproducing-badge')).toHaveLength(1)
    expect(screen.getAllByText('Ativa').length).toBeGreaterThanOrEqual(3)
  })

  it('código PAYER_* ainda desconhecido usa a mensagem do envelope e o caminho para corrigir', () => {
    const err = toApiError(422, { error: { code: 'PAYER_CODIGO_FUTURO', message: 'Dado do pagador recusado pelo meio de pagamento.', details: {} } })
    renderWithProviders(<CheckoutError error={err} />)
    const alert = screen.getByTestId('checkout-error-422')
    expect(alert).toHaveAttribute('data-code', 'PAYER_CODIGO_FUTURO')
    expect(alert).toHaveTextContent('Revise os dados do pagador')
    expect(alert).toHaveTextContent('Dado do pagador recusado pelo meio de pagamento.')
    expect(within(alert).getByRole('link', { name: 'Completar meus dados' })).toBeInTheDocument()
  })
})
