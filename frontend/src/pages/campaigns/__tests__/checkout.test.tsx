import { beforeEach, describe, expect, it } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router-dom'
import CartPage from '../CartPage'
import CheckoutPage from '../CheckoutPage'
import { headersOf, jsonResponse, mockFetch, renderWithProviders } from '@/test/utils'
import { campaignFixture, quoteFixture, availabilityFixture, sentBodies, signInAs } from './fixtures'

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

function renderCart() {
  return renderWithProviders(
    <Routes>
      <Route path="/anunciante/carrinho" element={<CartPage />} />
      <Route path="/anunciante/checkout/:id" element={<p>tela de pagamento do pedido</p>} />
    </Routes>,
    { route: '/anunciante/carrinho?c=c1' },
  )
}

type Responder = () => Response | Promise<Response>

function cartBackend(checkoutResponses: Responder[]) {
  let i = 0
  return mockFetch((url, init) => {
    if (url.includes('/checkout/orders')) return jsonResponse({ items: [], total: 0, page: 1, page_size: 20 })
    if (url.endsWith('/checkout') && init.method === 'POST') {
      const r = checkoutResponses[Math.min(i, checkoutResponses.length - 1)]
      i++
      return r()
    }
    if (url.endsWith('/campaigns/c1/quote')) return jsonResponse({ quote: quoteFixture(50000), availability: availabilityFixture })
    if (url.includes('/campaigns?')) return jsonResponse({ items: [campaignFixture()], total: 1, page: 1, page_size: 100 })
    return jsonResponse({ items: [] })
  })
}

function checkoutCalls(fetchMock: ReturnType<typeof mockFetch>) {
  return fetchMock.mock.calls.filter(([u, init]) => String(u).endsWith('/checkout') && (init as RequestInit).method === 'POST')
}

beforeEach(() => signInAs('advertiser'))

describe('carrinho / checkout', () => {
  it('mostra o total da cotação do servidor e o cartão como indisponível', async () => {
    cartBackend([() => jsonResponse({})])
    renderCart()
    await waitFor(() => expect(screen.getByTestId('cart-total')).toHaveTextContent('R$ 500,00'))
    expect(screen.getByTestId('card-unavailable')).toHaveTextContent('Pagamento por cartão indisponível neste ambiente')
    expect(screen.getByRole('radio', { name: /Cartão de crédito/ })).toBeDisabled()
  })

  it('409 de inventário: motivos agrupados por tela, sem nomes; nova tentativa usa NOVA Idempotency-Key', async () => {
    const fetchMock = cartBackend([
      () =>
        jsonResponse(
          {
            error: {
              code: 'INVENTORY_UNAVAILABLE',
              message: 'Não há inventário suficiente para a contratação nas telas solicitadas.',
              details: {
                available: 10,
                requested: 12,
                screens: [
                  { screen_id: 's1', reason: 'CAPACITY', message: 'Sem capacidade na faixa contratada' },
                  { screen_id: 's2', reason: 'CAPACITY', message: 'Sem capacidade na faixa contratada' },
                ],
              },
            },
          },
          409,
        ),
      () => jsonResponse({ order_id: 'o1', status: 'AGUARDANDO_PAGAMENTO', kind: 'CHECKOUT', total_cents: 50000, expires_at: '2099-01-01T14:00:00Z', payment: {} }, 201),
    ])
    const user = userEvent.setup()
    renderCart()
    await waitFor(() => expect(screen.getByTestId('cart-total')).toHaveTextContent('R$ 500,00'))
    await user.click(screen.getByRole('button', { name: 'Gerar cobrança PIX' }))
    const alert = await screen.findByTestId('checkout-error-409')
    expect(within(alert).getByText('Inventário indisponível')).toBeInTheDocument()
    expect(within(alert).getByText('2 tela(s): Sem capacidade na faixa contratada')).toBeInTheDocument()
    expect(within(alert).getByText(/Telas disponíveis: 10 de 12/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Gerar cobrança PIX' }))
    expect(await screen.findByText('tela de pagamento do pedido')).toBeInTheDocument()
    const calls = checkoutCalls(fetchMock)
    expect(calls).toHaveLength(2)
    const k1 = headersOf(calls[0][1] as RequestInit)['Idempotency-Key']
    const k2 = headersOf(calls[1][1] as RequestInit)['Idempotency-Key']
    expect(k1).toMatch(UUID)
    expect(k2).toMatch(UUID)
    expect(k1).not.toBe(k2)
    // corpo do checkout: só campanhas (e cupom) — nunca valores
    const body = sentBodies(fetchMock).find((b) => 'campaign_ids' in b)
    expect(body).toEqual({ campaign_ids: ['c1'] })
  })

  it('422 dados do pagador incompletos: lista o que falta e oferece completar o cadastro', async () => {
    cartBackend([
      () =>
        jsonResponse(
          { error: { code: 'PAYER_DATA_INCOMPLETE', message: 'Complete o cadastro (e-mail, telefone e CPF/CNPJ) para pagar.', details: { missing: ['phone', 'document'] } } },
          422,
        ),
    ])
    const user = userEvent.setup()
    renderCart()
    await waitFor(() => expect(screen.getByTestId('cart-total')).toHaveTextContent('R$ 500,00'))
    await user.click(screen.getByRole('button', { name: 'Gerar cobrança PIX' }))
    const alert = await screen.findByTestId('checkout-error-422')
    expect(within(alert).getByText(/Faltando: telefone, CPF\/CNPJ/)).toBeInTheDocument()
    expect(within(alert).getByRole('link', { name: 'Completar meus dados' })).toHaveAttribute(
      'href',
      `/minha-conta?voltar=${encodeURIComponent('/anunciante/carrinho?c=c1')}`,
    )
  })

  it('cupom aplicado vai para o endereço; a volta de "Completar meus dados" reaplica o cupom (validado no servidor)', async () => {
    const couponCalls: string[] = []
    const fetchMock = mockFetch((url, init) => {
      if (url.endsWith('/pricing/coupons/validate')) {
        const code = String(JSON.parse(String(init.body)).code)
        couponCalls.push(code)
        return jsonResponse({ valid: true, code, discount_type: 'PERCENT', discount_bps: 1000, discount_cents: 0, min_order_cents: 0, valid_until: '2027-01-05T22:45:00Z' })
      }
      if (url.includes('/checkout/orders')) return jsonResponse({ items: [], total: 0, page: 1, page_size: 20 })
      if (url.endsWith('/checkout') && init.method === 'POST')
        return jsonResponse({ error: { code: 'PAYER_DATA_INCOMPLETE', message: 'Complete o cadastro (e-mail, telefone e CPF/CNPJ) para pagar.', details: { missing: ['phone'] } } }, 422)
      if (url.endsWith('/campaigns/c1/quote')) return jsonResponse({ quote: quoteFixture(50000), availability: availabilityFixture })
      if (url.includes('/campaigns?')) return jsonResponse({ items: [campaignFixture()], total: 1, page: 1, page_size: 100 })
      return jsonResponse({ items: [] })
    })
    const user = userEvent.setup()
    renderWithProviders(
      <Routes>
        <Route path="/anunciante/carrinho" element={<CartPage />} />
      </Routes>,
      { route: '/anunciante/carrinho?c=c1&cupom=DEMO10' },
    )
    // volta com cupom no endereço: campo preenchido e cupom revalidado no servidor (uma vez)
    expect(screen.getByRole('textbox', { name: 'Cupom' })).toHaveValue('DEMO10')
    expect(await screen.findByText(/10% de desconto/)).toBeInTheDocument()
    expect(couponCalls).toEqual(['DEMO10'])
    await waitFor(() => expect(screen.getByTestId('cart-total')).toHaveTextContent('R$ 500,00'))
    await user.click(screen.getByRole('button', { name: 'Gerar cobrança PIX' }))
    const alert = await screen.findByTestId('checkout-error-422')
    const href = within(alert).getByRole('link', { name: 'Completar meus dados' }).getAttribute('href') ?? ''
    expect(decodeURIComponent(href.split('voltar=')[1] ?? '')).toBe('/anunciante/carrinho?c=c1&cupom=DEMO10')
    expect(sentBodies({ mock: { calls: checkoutCalls(fetchMock) } })[0]).toMatchObject({ coupon_code: 'DEMO10' })
  })

  it('rascunho com início no passado: aviso para revisar as datas antes de pagar', async () => {
    mockFetch((url) => {
      if (url.includes('/checkout/orders')) return jsonResponse({ items: [], total: 0, page: 1, page_size: 20 })
      if (url.endsWith('/campaigns/c1/quote')) return jsonResponse({ quote: quoteFixture(50000), availability: availabilityFixture })
      if (url.includes('/campaigns?'))
        return jsonResponse({ items: [campaignFixture({ start_date: '2020-01-06', end_date: '2020-02-04' })], total: 1, page: 1, page_size: 100 })
      return jsonResponse({ items: [] })
    })
    renderCart()
    expect(await screen.findByTestId('cart-start-past')).toHaveTextContent('O início (06/01/2020) já passou')
  })

  it('503 dependência indisponível: explica que nada foi cobrado nem reservado', async () => {
    cartBackend([
      () =>
        jsonResponse({ error: { code: 'CHECKOUT_DEPENDENCY_UNAVAILABLE', message: 'Checkout indisponível no momento.', details: {} } }, 503),
    ])
    const user = userEvent.setup()
    renderCart()
    await waitFor(() => expect(screen.getByTestId('cart-total')).toHaveTextContent('R$ 500,00'))
    await user.click(screen.getByRole('button', { name: 'Gerar cobrança PIX' }))
    const alert = await screen.findByTestId('checkout-error-503')
    expect(alert).toHaveTextContent('Nada foi cobrado nem reservado')
  })

  it('falha de rede: a repetição reaproveita a MESMA Idempotency-Key (sem cobrança duplicada)', async () => {
    const fetchMock = cartBackend([
      () => {
        throw new TypeError('Failed to fetch')
      },
      () => jsonResponse({ order_id: 'o9', status: 'AGUARDANDO_PAGAMENTO', kind: 'CHECKOUT', total_cents: 50000, expires_at: '2099-01-01T14:00:00Z', payment: {} }, 200),
    ])
    const user = userEvent.setup()
    renderCart()
    await waitFor(() => expect(screen.getByTestId('cart-total')).toHaveTextContent('R$ 500,00'))
    await user.click(screen.getByRole('button', { name: 'Gerar cobrança PIX' }))
    await screen.findByText(/Sem conexão com o servidor/)
    await user.click(screen.getByRole('button', { name: 'Gerar cobrança PIX' }))
    await screen.findByText('tela de pagamento do pedido')
    const calls = checkoutCalls(fetchMock)
    expect(calls).toHaveLength(2)
    expect(headersOf(calls[0][1] as RequestInit)['Idempotency-Key']).toBe(headersOf(calls[1][1] as RequestInit)['Idempotency-Key'])
  })
})

describe('página de pagamento', () => {
  const order = {
    id: 'o1',
    org_id: 'org-1',
    kind: 'CHECKOUT',
    status: 'AGUARDANDO_PAGAMENTO',
    media_cents: 50000,
    fees_cents: 0,
    discount_cents: 0,
    credit_applied_cents: 0,
    total_cents: 50000,
    payment_order_id: 'po1',
    expires_at: new Date(Date.now() + 2 * 3600 * 1000).toISOString(),
    paid_at: null,
    created_at: '2099-01-01T12:00:00Z',
    items: [{ campaign_id: 'c1', amount_cents: 50000, media_cents: 50000, fees_cents: 0, discount_cents: 0, quote_hash: 'h', quote: null }],
    payment: { id: 'po1', status: 'PENDING', amount_cents: 50000, method: 'pix', pix_qr_code: '00020126580014BR.GOV.BCB.PIX0136teste-pix-copia-e-cola5204000053039865406500.005802BR6304ABCD' },
  }

  it('PIX com QR gerado localmente, copia e cola e contagem regressiva do servidor', async () => {
    const fetchMock = mockFetch((url) => {
      if (url.endsWith('/checkout/orders/o1')) return jsonResponse(order)
      if (url.endsWith('/campaigns/c1')) return jsonResponse(campaignFixture({ status: 'AGUARDANDO_PAGAMENTO' }))
      return jsonResponse({})
    })
    renderWithProviders(
      <Routes>
        <Route path="/anunciante/checkout/:id" element={<CheckoutPage />} />
      </Routes>,
      { route: '/anunciante/checkout/o1' },
    )
    expect(await screen.findByTestId('order-total')).toHaveTextContent('R$ 500,00')
    expect(screen.getByRole('img', { name: 'QR code PIX para pagamento' })).toBeInTheDocument()
    expect(screen.getByDisplayValue(order.payment.pix_qr_code)).toBeInTheDocument()
    expect(screen.getByTestId('countdown')).toHaveTextContent(/Reserva expira em/)
    expect(screen.getByTestId('card-unavailable')).toHaveTextContent('Pagamento por cartão indisponível neste ambiente.')
    // nenhuma chamada a serviço externo de QR
    await waitFor(() => expect(fetchMock.mock.calls.every(([u]) => String(u).startsWith('/api/v1/'))).toBe(true))
  })

  it.each([
    ['CHECKOUT', 'As campanhas seguem para a análise da RD'],
    ['RENEWAL', 'A renovação foi aplicada com as mesmas telas, anúncios e programação, sem nova análise.'],
    ['CHANGE', 'A alteração segue para a análise da RD'],
  ])('pedido %s pago: texto do próximo passo conforme o tipo; desconto zero sem sinal', async (kind, next) => {
    const paid = { ...order, kind, status: 'PAGO', paid_at: '2026-10-08T12:00:00Z', payment: { ...order.payment, status: 'PAID' } }
    mockFetch((url) => {
      if (url.endsWith('/checkout/orders/o1')) return jsonResponse(paid)
      if (url.endsWith('/campaigns/c1')) return jsonResponse(campaignFixture({ status: 'ATIVA' }))
      return jsonResponse({})
    })
    renderWithProviders(
      <Routes>
        <Route path="/anunciante/checkout/:id" element={<CheckoutPage />} />
      </Routes>,
      { route: '/anunciante/checkout/o1' },
    )
    expect(await screen.findByText(next, { exact: false })).toBeInTheDocument()
    const discount = screen.getByText('Descontos').nextElementSibling
    expect(discount).toHaveTextContent('R$ 0,00')
    expect(discount?.textContent).not.toContain('-')
  })
})
