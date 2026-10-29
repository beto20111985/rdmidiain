import { beforeEach, describe, expect, it } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router-dom'
import CartPage from '../CartPage'
import MarginPage from '../MarginPage'
import { RenewalCard, RenewDialog } from '../detail/ActionDialogs'
import { CheckoutError } from '../checkout/shared'
import { describeError, priceFloorText } from '../lib/errorText'
import { toApiError } from '@/lib/errors'
import { headersOf, jsonResponse, mockFetch, renderWithProviders } from '@/test/utils'
import { availabilityFixture, campaignFixture, quoteFixture, signInAs } from './fixtures'

const NOT_SUPPORTED = {
  error: {
    code: 'PAYER_DOCUMENT_NOT_SUPPORTED_BY_GATEWAY',
    message: 'O meio de pagamento (CloudPayPag) ainda não aceita CNPJ alfanumérico. Seu cadastro está correto; entre em contato com o suporte da RD para concluir o pagamento.',
    details: { document_type: 'CNPJ', format: 'ALPHANUMERIC' },
  },
}

describe('documento do pagador (W-FIN)', () => {
  beforeEach(() => signInAs('advertiser'))

  it('checkout: CNPJ alfanumérico → orienta o suporte, sem pedir para alterar o cadastro; carrinho e cupom preservados', async () => {
    mockFetch((url, init) => {
      if (url.includes('/checkout/orders')) return jsonResponse({ items: [], total: 0, page: 1, page_size: 20 })
      if (url.endsWith('/pricing/coupons/validate'))
        return jsonResponse({ valid: true, code: 'BEMVINDO', discount_type: 'PERCENT', discount_bps: 1000, discount_cents: 0, min_order_cents: 0, valid_until: '2099-01-01T02:59:59Z' })
      if (url.endsWith('/checkout') && init.method === 'POST') return jsonResponse(NOT_SUPPORTED, 422)
      if (url.endsWith('/campaigns/c1/quote')) return jsonResponse({ quote: quoteFixture(50000), availability: availabilityFixture })
      if (url.includes('/campaigns?')) return jsonResponse({ items: [campaignFixture()], total: 1, page: 1, page_size: 100 })
      return jsonResponse({})
    })
    const user = userEvent.setup()
    renderWithProviders(
      <Routes>
        <Route path="/anunciante/carrinho" element={<CartPage />} />
      </Routes>,
      { route: '/anunciante/carrinho?c=c1' },
    )
    await waitFor(() => expect(screen.getByTestId('cart-total')).toHaveTextContent('R$ 500,00'))
    fireEvent.change(screen.getByRole('textbox', { name: 'Cupom' }), { target: { value: 'bemvindo' } })
    await user.click(screen.getByRole('button', { name: 'Aplicar' }))
    expect(await screen.findByText(/10\s?% de desconto/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Gerar cobrança PIX' }))
    const alert = await screen.findByTestId('checkout-error-422')
    expect(alert).toHaveAttribute('data-code', 'PAYER_DOCUMENT_NOT_SUPPORTED_BY_GATEWAY')
    expect(within(alert).getByText('CNPJ alfanumérico ainda não aceito pelo meio de pagamento')).toBeInTheDocument()
    expect(within(alert).getByRole('link', { name: 'Falar com o suporte' })).toHaveAttribute('href', '/anunciante/suporte')
    expect(within(alert).queryByRole('link', { name: 'Completar meus dados' })).not.toBeInTheDocument()
    expect(within(alert).getByText(/não altere o CNPJ/)).toBeInTheDocument()
    // estado preservado junto à ação
    expect(screen.getByRole('textbox', { name: 'Cupom' })).toHaveValue('bemvindo')
    expect(screen.getByRole('checkbox', { name: 'Campanha de Teste' })).toBeChecked()
    expect(screen.getByRole('button', { name: 'Gerar cobrança PIX' })).toBeEnabled()
  })

  it.each([
    ['PAYER_DOCUMENT_INVALID', 'CPF/CNPJ do cadastro inválido para pagamento', 'CNPJ do cadastro inválido para pagamento (14 dígitos). Corrija o cadastro.', {}, false],
    ['PAYER_DATA_INCOMPLETE', 'Complete o cadastro para pagar', 'Complete o cadastro (e-mail, telefone e CPF/CNPJ) para pagar.', {}, true],
    ['PAYER_DATA_INCOMPLETE', 'Complete o cadastro para pagar', 'Complete o cadastro (CPF/CNPJ) para pagar.', { missing: ['document'] }, false],
  ])('%s: título claro, mensagem do servidor e o caminho que de fato corrige (%#)', (code, title, message, details, accountLink) => {
    renderWithProviders(<CheckoutError error={toApiError(422, { error: { code, message, details } })} />)
    const alert = screen.getByTestId('checkout-error-422')
    expect(alert).toHaveTextContent(title)
    expect(alert).toHaveTextContent(message)
    // telefone: Minha conta; CPF/CNPJ da empresa: só o suporte (sem link para tela que não corrige)
    expect(within(alert).queryByRole('link', { name: 'Completar meus dados' }) !== null).toBe(accountLink)
    expect(within(alert).getByRole('link', { name: 'Atualizar dados da empresa com o suporte' })).toHaveAttribute('href', '/anunciante/suporte')
  })

  it('renovação: a proposta não reserva; o checkout da renovação manda Idempotency-Key e mostra o erro do pagador no próprio cartão', async () => {
    const proposal = {
      campaign_id: 'c1', revision_id: 'r9', revision: 9, stage: 'PROPOSTA', revision_status: 'AGUARDANDO_PAGAMENTO', auto: true, periods: 1,
      previous_end_date: '2026-10-31', new_end_date: '2026-11-30', extension_start: '2026-11-01', screens_count: 12, total_cents: 99000,
      deadline: '2026-11-01T02:59:59Z', quote: null, order: null,
    }
    const fetchMock = mockFetch((url, init) => {
      if (url.endsWith('/campaigns/c1/renewal')) return jsonResponse(proposal)
      if (url.endsWith('/campaigns/c1/renew/checkout') && init.method === 'POST')
        return jsonResponse({ error: { code: 'PAYER_DOCUMENT_INVALID', message: 'CNPJ do cadastro inválido para pagamento (14 dígitos). Corrija o cadastro.', details: {} } }, 422)
      return jsonResponse({})
    })
    const user = userEvent.setup()
    renderWithProviders(<RenewalCard campaignId="c1" canPay checkoutPath={(id) => `/anunciante/checkout/${id}`} onCheckout={() => undefined} />)
    const card = await screen.findByTestId('renewal-card')
    expect(card).toHaveTextContent('Renovação automática · Proposta — sem reserva de telas e sem cobrança ainda')
    expect(within(card).getByTestId('renewal-total')).toHaveTextContent('R$ 990,00')
    expect(card).toHaveTextContent(/ficam reservadas por 2 horas/)
    await user.click(within(card).getByRole('button', { name: 'Gerar cobrança PIX da renovação' }))
    expect(await within(card).findByText('CPF/CNPJ do cadastro inválido para pagamento')).toBeInTheDocument()
    const call = fetchMock.mock.calls.find(([u]) => String(u).endsWith('/renew/checkout'))!
    expect(headersOf(call[1] as RequestInit)['Idempotency-Key']).toMatch(/^[0-9a-f-]{36}$/)
    expect(JSON.parse((call[1] as RequestInit).body as string)).toEqual({ method: 'pix' })
  })

  it('renovação: o diálogo só calcula a proposta (sem Idempotency-Key, sem cobrança) e mantém os períodos em erro', async () => {
    const fetchMock = mockFetch((url, init) => {
      if (url.endsWith('/campaigns/c1/renew') && init.method === 'POST')
        return jsonResponse({ error: { code: 'RENEWAL_WINDOW_CLOSED', message: 'O período vigente já terminou; contrate uma nova campanha.', details: {} } }, 409)
      return jsonResponse({})
    })
    const user = userEvent.setup()
    renderWithProviders(
      <RenewDialog campaign={campaignFixture({ status: 'ATIVA', period_type: 'MENSAL' })} open onOpenChange={() => undefined} onDone={() => undefined} />,
    )
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent(/só ficam reservadas \(por 2 horas\) quando você gerar a cobrança/)
    fireEvent.change(within(dialog).getByRole('spinbutton', { name: 'Quantidade de períodos' }), { target: { value: '3' } })
    await user.click(within(dialog).getByRole('button', { name: 'Calcular proposta de renovação' }))
    expect(await within(dialog).findByText('O período vigente já terminou; contrate uma nova campanha.')).toBeInTheDocument()
    expect(within(dialog).getByRole('spinbutton', { name: 'Quantidade de períodos' })).toHaveValue(3)
    const call = fetchMock.mock.calls.find(([u]) => String(u).endsWith('/campaigns/c1/renew'))!
    expect(headersOf(call[1] as RequestInit)['Idempotency-Key']).toBeUndefined()
    expect(JSON.parse((call[1] as RequestInit).body as string)).toEqual({ periods: 3 })
  })
})

describe('piso de preço RD (white-label)', () => {
  it('formata o mínimo em BRL a partir de min_price_cents (contrato único PRICE_BELOW_RD_MINIMUM)', () => {
    const fromInstance = toApiError(400, {
      error: { code: 'PRICE_BELOW_RD_MINIMUM', message: 'Preço abaixo do mínimo da RD.', details: { floor_key: 'slot_30s_insercao', min_price_cents: 150, price_cents: 90 } },
    })
    expect(priceFloorText(fromInstance)?.replace(/\u00a0/g, ' ')).toBe('Preço abaixo do mínimo da RD: o mínimo é R$ 1,50 e o valor calculado foi R$ 0,90. Ajuste preços, descontos ou a configuração.')
    const alias = toApiError(422, { error: { code: 'PRICE_BELOW_RD_FLOOR', message: 'Mensagem antiga.', details: { min_price_cents: 100000 } } })
    expect(priceFloorText(alias)).toBeNull() // alias removido: contrato único é PRICE_BELOW_RD_MINIMUM
    expect(describeError(toApiError(409, { error: { code: 'OUTRO', message: 'Mensagem do servidor.', details: {} } }))).toBe('Mensagem do servidor.')
  })

  it('checkout com piso violado mostra o mínimo em BRL', () => {
    renderWithProviders(
      <CheckoutError
        error={toApiError(400, { error: { code: 'PRICE_BELOW_RD_MINIMUM', message: 'x', details: { floor_key: 'slot_30s_mensal_tela', min_price_cents: 4990, price_cents: 3000 } } })}
      />,
    )
    expect(screen.getByTestId('checkout-error-floor').textContent?.replace(/\u00a0/g, ' ')).toContain('o mínimo é R$ 49,90 e o valor calculado foi R$ 30,00')
  })

  it('margem: validação do piso no servidor exibe o mínimo formatado', async () => {
    signInAs('staff')
    mockFetch((url) => {
      if (url.endsWith('/instance/price-floors'))
        return jsonResponse({ items: [{ key: 'slot_30s_insercao', label: 'Inserção de 30 s', unit: 'inserção', min_price_cents: 150, note: null, version: 1, updated_at: null }] })
      if (url.endsWith('/instance/price-floors/check'))
        return jsonResponse({ error: { code: 'PRICE_BELOW_RD_MINIMUM', message: 'Preço abaixo do mínimo da RD.', details: { floor_key: 'slot_30s_insercao', min_price_cents: 150, price_cents: 100 } } }, 400)
      return jsonResponse({})
    })
    const user = userEvent.setup()
    renderWithProviders(<MarginPage />)
    await screen.findAllByText('Inserção de 30 s')
    await user.click(screen.getByRole('combobox', { name: 'Item' }))
    await user.click(await screen.findByRole('option', { name: 'Inserção de 30 s' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Preço da instância (R$)' }), { target: { value: '1,00' } })
    await user.click(screen.getByRole('button', { name: 'Validar no servidor' }))
    expect(await screen.findByText(/o mínimo é R\$ 1,50 e o valor calculado foi R\$ 1,00/)).toBeInTheDocument()
  })
})
