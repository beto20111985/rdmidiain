import { beforeEach, describe, expect, it } from 'vitest'
import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router-dom'
import CampaignWizardPage from '../CampaignWizardPage'
import { ALLOWED_DURATIONS, durationSchema, isAllowedDuration, serverFieldErrors } from '../lib/rules'
import { toApiError } from '@/lib/errors'
import { jsonResponse, mockFetch, renderWithProviders } from '@/test/utils'
import { availabilityFixture, campaignFixture, quoteFixture, sentBodies, signInAs } from './fixtures'

const PRICE_KEYS = ['amount_cents', 'price', 'price_cents', 'total_cents', 'total', 'media_cents']

function renderWizard(route: string) {
  return renderWithProviders(
    <Routes>
      <Route path="/anunciante/campanhas/nova" element={<CampaignWizardPage />} />
      <Route path="/campanhas/nova" element={<CampaignWizardPage />} />
      <Route path="/anunciante/carrinho" element={<p>página do carrinho</p>} />
    </Routes>,
    { route },
  )
}

beforeEach(() => signInAs('advertiser'))

describe('duração do anúncio: 30, 60 ou 120 s', () => {
  it.each([15, 45, 90, 31, 0, 240])('rejeita %i s no cliente', (d) => {
    const r = durationSchema.safeParse(d)
    expect(r.success).toBe(false)
    expect(isAllowedDuration(d)).toBe(false)
    if (!r.success) expect(r.error.issues[0].message).toBe('Duração permitida: 30, 60 ou 120 segundos.')
  })

  it.each([30, 60, 120])('aceita %i s', (d) => {
    expect(durationSchema.safeParse(d).success).toBe(true)
  })

  it('o seletor só oferece 30, 60 e 120 segundos', async () => {
    mockFetch((url) => (url.endsWith('/campaigns/c1') ? jsonResponse(campaignFixture()) : jsonResponse({ items: [] })))
    renderWizard('/anunciante/campanhas/nova?id=c1&etapa=2')
    await screen.findByText('30 segundos')
    expect(ALLOWED_DURATIONS).toEqual([30, 60, 120])
    expect(screen.getByText('60 segundos')).toBeInTheDocument()
    expect(screen.getByText('120 segundos')).toBeInTheDocument()
    for (const bad of [15, 45, 90]) expect(screen.queryByText(`${bad} segundos`)).not.toBeInTheDocument()
  })

  it('duração legada inválida (45 s) é barrada no cliente e NÃO é enviada', async () => {
    const fetchMock = mockFetch((url) => (url.endsWith('/campaigns/c1') ? jsonResponse(campaignFixture({ duration_s: 45 })) : jsonResponse({ items: [] })))
    const user = userEvent.setup()
    renderWizard('/anunciante/campanhas/nova?id=c1&etapa=2')
    await user.click(await screen.findByRole('button', { name: 'Salvar rascunho' }))
    expect(await screen.findByText('Duração permitida: 30, 60 ou 120 segundos.')).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === 'PATCH')).toBe(false)
  })

  it('erro DURATION_NOT_ALLOWED do servidor aparece junto ao campo de duração', async () => {
    const fetchMock = mockFetch((url, init) => {
      if (url.endsWith('/campaigns/c1') && init.method === 'PATCH')
        return jsonResponse(
          { error: { code: 'DURATION_NOT_ALLOWED', message: 'Duração permitida: 30, 60 ou 120 segundos.', details: { duration_s: 60 } } },
          400,
        )
      if (url.endsWith('/campaigns/c1')) return jsonResponse(campaignFixture())
      return jsonResponse({ items: [] })
    })
    const user = userEvent.setup()
    renderWizard('/anunciante/campanhas/nova?id=c1&etapa=2')
    await user.click(await screen.findByText('60 segundos'))
    await user.click(screen.getByRole('button', { name: 'Salvar rascunho' }))
    const msg = await screen.findByText('Duração permitida: 30, 60 ou 120 segundos.')
    // mensagem dentro do grupo do campo (não num alerta genérico)
    expect(msg.closest('[data-slot="field"]')).not.toBeNull()
    expect(screen.queryByText('Não foi possível salvar')).not.toBeInTheDocument()
    const patch = sentBodies(fetchMock).find((b) => 'duration_s' in b)
    expect(patch).toMatchObject({ duration_s: 60, expected_version: 3 })
  })

  it('serverFieldErrors mapeia códigos de negócio e 422 por campo', () => {
    const e400 = toApiError(400, { error: { code: 'DURATION_MISMATCH', message: 'O vídeo tem 31,20 s…', details: {} } })
    expect(serverFieldErrors(e400)).toEqual({ duration_s: 'O vídeo tem 31,20 s…' })
    const e422 = toApiError(422, { error: { code: 'VALIDATION_ERROR', message: 'x', details: { errors: [{ loc: ['body', 'name'], msg: 'curto' }] } } })
    expect(serverFieldErrors(e422)).toEqual({ name: 'curto' })
  })
})

describe('preço sempre calculado pelo servidor', () => {
  it('a revisão exibe o total da cotação do servidor e nenhuma requisição envia preço', async () => {
    const fetchMock = mockFetch((url, init) => {
      if (url.endsWith('/campaigns/c1/quote') && init.method === 'POST') return jsonResponse({ quote: quoteFixture(123456), availability: availabilityFixture })
      if (url.endsWith('/campaigns/c1')) return jsonResponse(campaignFixture())
      return jsonResponse({ items: [] })
    })
    renderWizard('/anunciante/campanhas/nova?id=c1&etapa=5')
    const total = await screen.findByTestId('quote-total')
    expect(total).toHaveTextContent('R$ 1.234,56')
    expect(screen.getByText(/tabela de preços v7/)).toBeInTheDocument()
    // anunciante não vê margem/custos
    expect(screen.queryByText(/Margem/)).not.toBeInTheDocument()
    for (const body of sentBodies(fetchMock)) for (const k of PRICE_KEYS) expect(body).not.toHaveProperty(k)
  })

  it('erro de cotação mostra a mensagem do servidor e leva à etapa a corrigir — nunca um preço local', async () => {
    mockFetch((url, init) => {
      if (url.endsWith('/campaigns/c1/quote') && init.method === 'POST')
        return jsonResponse({ error: { code: 'NO_SCREENS_AVAILABLE', message: 'Nenhuma tela disponível para a segmentação desta campanha.', details: {} } }, 409)
      if (url.includes('/campaigns/availability')) return jsonResponse(availabilityFixture)
      if (url.endsWith('/campaigns/c1')) return jsonResponse(campaignFixture())
      return jsonResponse({ items: [] })
    })
    const user = userEvent.setup()
    renderWizard('/anunciante/campanhas/nova?id=c1&etapa=5')
    expect(await screen.findByText('Nenhuma tela disponível para a segmentação desta campanha.')).toBeInTheDocument()
    expect(screen.queryByTestId('quote-total')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Corrigir na etapa 3' }))
    expect(await screen.findByText(/Etapa 3 de 5/)).toBeInTheDocument()
  })
})

describe('anunciante nunca vê nomes de estabelecimentos', () => {
  it('segmentação do anunciante: sem opção de estabelecimento específico e sem chamar /establishments', async () => {
    const fetchMock = mockFetch((url, init) => {
      if (url.includes('/campaigns/availability') && init.method === 'POST') return jsonResponse(availabilityFixture)
      if (url.endsWith('/campaigns/c1')) return jsonResponse(campaignFixture())
      if (url.includes('/taxonomy/nodes')) return jsonResponse({ items: [{ id: 'n1', parent_id: null, name: 'Academias', slug: 'academias', depth: 0, is_active: true, ancestor_ids: [], kind: 'SECTOR' }], total: 1, page: 1, page_size: 1 })
      return jsonResponse({ items: [] })
    })
    const user = userEvent.setup()
    renderWizard('/anunciante/campanhas/nova?id=c1&etapa=3')
    const panel = await screen.findByTestId('availability')
    expect(within(panel).getByText('12', { selector: 'dd' })).toBeInTheDocument()
    expect(within(panel).getByText(/Os nomes não são exibidos/)).toBeInTheDocument()
    await user.click(screen.getByRole('combobox', { name: 'Tipo' }))
    expect(await screen.findByRole('option', { name: 'Cidade' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'Estabelecimento específico' })).not.toBeInTheDocument()
    const urls = fetchMock.mock.calls.map(([u]) => String(u))
    expect(urls.some((u) => u.includes('/establishments'))).toBe(false)
    expect(urls.some((u) => u.includes('/conflicts'))).toBe(false)
    // disponibilidade pela MESMA elegibilidade do checkout: campaign_id (conflitos dos anúncios aplicados)
    const availCall = fetchMock.mock.calls.find(([u, i]) => String(u).includes('/campaigns/availability') && (i as RequestInit).method === 'POST')!
    const body = JSON.parse(String((availCall[1] as RequestInit).body)) as Record<string, unknown>
    expect(body.campaign_id).toBe('c1')
    expect(body).not.toHaveProperty('org_id')
  })

  it('a equipe RD tem a opção de estabelecimento específico', async () => {
    signInAs('staff')
    mockFetch((url, init) => {
      if (url.includes('/campaigns/availability') && init.method === 'POST') return jsonResponse(availabilityFixture)
      if (url.endsWith('/campaigns/c1/conflicts')) return jsonResponse({ allowed_screens: 12, blocked_establishments: 0, blocked: [] })
      if (url.endsWith('/campaigns/c1')) return jsonResponse(campaignFixture())
      return jsonResponse({ items: [], total: 0, page: 1, page_size: 1 })
    })
    const user = userEvent.setup()
    renderWizard('/campanhas/nova?id=c1&etapa=3')
    await screen.findByTestId('availability')
    await user.click(screen.getByRole('combobox', { name: 'Tipo' }))
    expect(await screen.findByRole('option', { name: 'Estabelecimento específico' })).toBeInTheDocument()
  })
})
