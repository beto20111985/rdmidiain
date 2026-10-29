import { beforeEach, describe, expect, it } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router-dom'
import CampaignDetailPage from '../CampaignDetailPage'
import ApprovalQueuePage from '../ApprovalQueuePage'
import { headersOf, jsonResponse, mockFetch, renderWithProviders } from '@/test/utils'
import { campaignFixture, signInAs } from './fixtures'

const live = {
  id: 'r1',
  number: 1,
  kind: 'INITIAL',
  status: 'APROVADA',
  spec: {},
  screens_count: 2,
  creative_ids: [],
  price_cents: 90000,
  price_diff_cents: null,
  change_summary: null,
  quote: null,
  created_at: '2099-01-01T12:00:00Z',
  decided_at: '2099-01-02T12:00:00Z',
  decision_reason: null,
}

function detailBackend(screensResponse: unknown) {
  return mockFetch((url, init) => {
    if (url.endsWith('/campaigns/c1/kill') && init.method === 'POST') return jsonResponse(campaignFixture({ status: 'PAUSADA', has_live_version: true, live_revision: live }))
    if (url.endsWith('/campaigns/c1/screens')) return jsonResponse(screensResponse)
    if (url.endsWith('/campaigns/c1/history')) return jsonResponse({ items: [{ from: 'EM_ANALISE', to: 'APROVADA', trigger: 'approve', reason: null, origin: 'api', at: '2099-01-02T12:00:00Z' }] })
    if (url.endsWith('/campaigns/c1/revisions')) return jsonResponse({ items: [live] })
    if (url.endsWith('/campaigns/c1/conflicts')) return jsonResponse({ allowed_screens: 2, blocked_establishments: 1 })
    if (url.endsWith('/campaigns/c1/cancel-quote'))
      return jsonResponse({ campaign_id: 'c1', status: 'ATIVA', can_cancel: true, paid: true, refund_total_cents: 45000, payments: [{ payment_order_id: 'po1', campaign_id: 'c1', as_of: '2099-01-03', window_days: 7, policy: 'PROPORTIONAL', allowed: true, media_refund_cents: 45000, services_refund_cents: 0, refund_cents: 45000 }] })
    if (url.endsWith('/campaigns/c1'))
      return jsonResponse(
        campaignFixture({
          status: 'ATIVA',
          paid_at: '2099-01-01T12:30:00Z',
          contracted_price_cents: 90000,
          has_live_version: true,
          live_revision: live,
          allowed_transitions: { pause: ['PAUSADA'], cancel: ['CANCELADA'], kill: ['PAUSADA'] },
          is_reproducing: true,
          display_status: 'REPRODUZINDO',
          reproducing: { playing: true, last_valid_play_at: '2099-01-03T10:00:00Z', source: 'playback_service', window_minutes: 15 },
        }),
      )
    return jsonResponse({})
  })
}

function renderDetail(path: string) {
  return renderWithProviders(
    <Routes>
      <Route path="/anunciante/campanhas/:id" element={<CampaignDetailPage />} />
      <Route path="/campanhas/:id" element={<CampaignDetailPage />} />
    </Routes>,
    { route: path },
  )
}

describe('detalhe da campanha — anunciante', () => {
  beforeEach(() => signInAs('advertiser'))

  it('telas contratadas por código e UF, sem nome de estabelecimento (mesmo que o servidor envie IDs)', async () => {
    const fetchMock = detailBackend({
      source: 'CONTRATADAS',
      items: [
        { friendly_id: 'TEL-GO-7Q3M', state: 'GO', orientation: 'HORIZONTAL', establishment_id: 'e1', city_key: 'GO:goiania' },
        { friendly_id: 'TEL-SP-AB2C', state: 'SP', orientation: 'VERTICAL', establishment_id: 'e2', city_key: 'SP:sao-paulo' },
      ],
    })
    renderDetail('/anunciante/campanhas/c1')
    expect(await screen.findByText('TEL-GO-7Q3M')).toBeInTheDocument()
    expect(screen.getByText('TEL-SP-AB2C')).toBeInTheDocument()
    expect(screen.getByText('Por UF: GO (1), SP (1)')).toBeInTheDocument()
    expect(screen.queryByRole('columnheader', { name: 'Estabelecimento' })).not.toBeInTheDocument()
    expect(screen.getByText(/nomes não são exibidos/)).toBeInTheDocument()
    expect(screen.getAllByText('Reproduzindo').length).toBeGreaterThan(0)
    // sem kill para o anunciante
    expect(screen.queryByRole('button', { name: 'Kill emergencial' })).not.toBeInTheDocument()
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(3))
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes('/establishments'))).toBe(false)
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes('/organizations'))).toBe(false)
  })

  it('cancelar campanha paga mostra a cotação de reembolso calculada pelo servidor', async () => {
    detailBackend({ items: [] })
    const user = userEvent.setup()
    renderDetail('/anunciante/campanhas/c1')
    await user.click(await screen.findByRole('button', { name: 'Cancelar campanha' }))
    const dialog = await screen.findByRole('dialog')
    expect(await within(dialog).findByText(/Reembolso previsto: R\$ 450,00/)).toBeInTheDocument()
    expect(within(dialog).getByText(/proporcional ao período não veiculado/)).toBeInTheDocument()
    // exige nome digitado e motivo
    expect(within(dialog).getByRole('button', { name: 'Cancelar campanha' })).toBeDisabled()
  })
})

describe('detalhe da campanha — RD', () => {
  beforeEach(() => signInAs('staff'))

  it('kill emergencial exige motivo forte (≥ 20) e nome digitado; envia Idempotency-Key', async () => {
    const fetchMock = detailBackend({ items: [] })
    const user = userEvent.setup()
    renderDetail('/campanhas/c1')
    await user.click(await screen.findByRole('button', { name: 'Kill emergencial' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByRole('textbox', { name: /Motivo/ }), 'curto')
    await user.type(within(dialog).getByRole('textbox', { name: /Digite/ }), 'Campanha de Teste')
    await user.click(within(dialog).getByRole('button', { name: 'Interromper agora' }))
    expect(await within(dialog).findByText('Escreva ao menos 20 caracteres.')).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([u]) => String(u).endsWith('/kill'))).toBe(false)
    await user.type(within(dialog).getByRole('textbox', { name: /Motivo/ }), ' — conteúdo indevido denunciado pelo estabelecimento')
    await user.click(within(dialog).getByRole('button', { name: 'Interromper agora' }))
    await waitFor(() => expect(fetchMock.mock.calls.some(([u]) => String(u).endsWith('/campaigns/c1/kill'))).toBe(true))
    const call = fetchMock.mock.calls.find(([u]) => String(u).endsWith('/campaigns/c1/kill'))!
    expect(headersOf(call[1] as RequestInit)['Idempotency-Key']).toMatch(/^[0-9a-f-]{36}$/)
    expect(JSON.parse((call[1] as RequestInit).body as string).reason).toMatch(/^curto — conteúdo indevido/)
  })
})

describe('fila de aprovação', () => {
  beforeEach(() => signInAs('staff'))

  it('aprovação em massa envia Idempotency-Key e mostra o resultado por item', async () => {
    const item = (id: string, name: string) => ({ ...campaignFixture({ id, name, status: 'EM_ANALISE', review_due_at: '2099-01-02T12:00:00Z' }), sla_overdue: id === 'c2', is_change_request: false })
    const fetchMock = mockFetch((url, init) => {
      if (url.includes('/campaigns/review-queue')) return jsonResponse({ items: [item('c1', 'Alpha'), item('c2', 'Beta')], total: 2, page: 1, page_size: 25 })
      if (url.endsWith('/campaigns/approvals/bulk') && init.method === 'POST')
        return jsonResponse({ action: 'approve', done: ['c1'], failed: [{ campaign_id: 'c2', code: 'CREATIVES_NOT_READY', message: 'Há criativos inválidos ou ausentes.' }] })
      return jsonResponse({})
    })
    const user = userEvent.setup()
    renderWithProviders(<ApprovalQueuePage />, { route: '/campanhas/aprovacoes' })
    expect(await screen.findByText('Alpha')).toBeInTheDocument()
    expect(screen.getByText(/Atrasada/)).toBeInTheDocument()
    await user.click(screen.getByRole('checkbox', { name: 'Selecionar Alpha' }))
    await user.click(screen.getByRole('checkbox', { name: 'Selecionar Beta' }))
    await user.click(screen.getByRole('button', { name: 'Aprovar selecionadas (2)' }))
    await user.click(await screen.findByRole('button', { name: 'Aprovar selecionadas' }))
    expect(await screen.findByText(/1 concluída\(s\), 1 com falha/)).toBeInTheDocument()
    expect(screen.getByText('Beta: Há criativos inválidos ou ausentes.')).toBeInTheDocument()
    const call = fetchMock.mock.calls.find(([u]) => String(u).endsWith('/campaigns/approvals/bulk'))!
    expect(headersOf(call[1] as RequestInit)['Idempotency-Key']).toMatch(/^[0-9a-f-]{36}$/)
    expect(JSON.parse((call[1] as RequestInit).body as string)).toEqual({ campaign_ids: ['c1', 'c2'], action: 'approve', reason: null })
  })
})
