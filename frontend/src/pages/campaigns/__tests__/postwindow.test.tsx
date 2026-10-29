import { beforeEach, describe, expect, it } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ApprovalQueuePage from '../ApprovalQueuePage'
import { CreativeReviewTab } from '../approvals/CreativeReviewTab'
import { CHANGE_NO_CAPACITY_TEXT } from '../lib/errorText'
import { headersOf, jsonResponse, mockFetch, renderWithProviders } from '@/test/utils'
import { availabilityFixture, campaignFixture, quoteFixture, signInAs } from './fixtures'
import { ChangeRequestDialog } from '../detail/ChangeRequestDialog'
import { useCampaignArea } from '../lib/hooks'
import type { CampaignDetail } from '../lib/types'

const NO_CAPACITY = {
  error: {
    code: 'CHANGE_NO_CAPACITY',
    message: 'Sem capacidade para aplicar a alteração.',
    details: { cause: 'INVENTORY_UNAVAILABLE' },
  },
}

beforeEach(() => signInAs('staff'))

describe('alteração sem custo: aprovação sem capacidade (409 CHANGE_NO_CAPACITY)', () => {
  it('aprovação individual: mensagem clara no diálogo; a alteração segue em análise com a versão vigente', async () => {
    const item = { ...campaignFixture({ id: 'c1', name: 'Alteração Y', status: 'EM_ANALISE', has_live_version: true, has_pending_change: true }), sla_overdue: false, is_change_request: true }
    const fetchMock = mockFetch((url, init) => {
      if (url.endsWith('/campaigns/c1/approve') && init.method === 'POST') return jsonResponse(NO_CAPACITY, 409)
      if (url.includes('/campaigns/review-queue')) return jsonResponse({ items: [item], total: 1, page: 1, page_size: 25 })
      return jsonResponse({})
    })
    const user = userEvent.setup()
    renderWithProviders(<ApprovalQueuePage />, { route: '/campanhas/aprovacoes' })
    expect(await screen.findByText('Alteração (versão anterior no ar)')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Aprovar' }))
    const dialog = await screen.findByRole('alertdialog')
    await user.click(within(dialog).getByRole('button', { name: 'Aprovar' }))
    expect(await within(dialog).findByText(CHANGE_NO_CAPACITY_TEXT)).toBeInTheDocument()
    const call = fetchMock.mock.calls.find(([u]) => String(u).endsWith('/campaigns/c1/approve'))!
    expect(headersOf(call[1] as RequestInit)['Idempotency-Key']).toMatch(/^[0-9a-f-]{36}$/)
  })

  it('fila: falha de capacidade na aprovação em massa aparece com a mensagem clara', async () => {
    const item = { ...campaignFixture({ id: 'c1', name: 'Alteração X', status: 'EM_ANALISE' }), sla_overdue: false, is_change_request: true }
    mockFetch((url, init) => {
      if (url.includes('/campaigns/review-queue')) return jsonResponse({ items: [item], total: 1, page: 1, page_size: 25 })
      if (url.endsWith('/campaigns/approvals/bulk') && init.method === 'POST')
        return jsonResponse({ action: 'approve', done: [], failed: [{ campaign_id: 'c1', code: 'CHANGE_NO_CAPACITY', message: 'Sem capacidade.' }] })
      return jsonResponse({})
    })
    const user = userEvent.setup()
    renderWithProviders(<ApprovalQueuePage />, { route: '/campanhas/aprovacoes' })
    await user.click(await screen.findByRole('checkbox', { name: 'Selecionar Alteração X' }))
    await user.click(screen.getByRole('button', { name: 'Aprovar selecionadas (1)' }))
    await user.click(await screen.findByRole('button', { name: 'Aprovar selecionadas' }))
    expect(await screen.findByText(`Alteração X: ${CHANGE_NO_CAPACITY_TEXT}`)).toBeInTheDocument()
  })
})

describe('revisão RD de anúncios internos/institucionais/fallback', () => {
  const creative = {
    id: 'k1', code: 'RD-ABCD23', org_id: 'org-1', name: 'Cardápio do dia', kind: 'INTERNAL', media_file_id: 'm1', duration_seconds: 30,
    orientation: 'HORIZONTAL', media_duration_ms: null, ad_text: null, status: 'EM_ANALISE', technical_status: 'VALIDO', technical_error: null,
    product_id: null, brand_id: null, segment_ids: [], version: 1, supersedes_id: null, root_id: null, rejection_reason: null,
    created_at: '2026-10-07T12:00:00Z', updated_at: '2026-10-07T12:00:00Z',
  }

  it('lista por tipo e aprova/rejeita via POST /creatives/{id}/review', async () => {
    const fetchMock = mockFetch((url, init) => {
      if (url.endsWith('/creatives/k1/review') && init.method === 'POST') return jsonResponse({ ...creative, status: 'REJEITADO' })
      if (url.includes('/creatives?')) return jsonResponse({ items: [creative], total: 1, page: 1, page_size: 25 })
      return jsonResponse({})
    })
    const user = userEvent.setup()
    renderWithProviders(<CreativeReviewTab />, { route: '/campanhas/aprovacoes' })
    expect(await screen.findByText('RD-ABCD23')).toBeInTheDocument()
    const listCall = fetchMock.mock.calls.find(([u]) => String(u).includes('/creatives?'))!
    expect(String(listCall[0])).toContain('kind=INTERNAL')
    expect(String(listCall[0])).toContain('status=RASCUNHO%2CEM_ANALISE')
    await user.click(screen.getByRole('button', { name: 'Rejeitar' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByRole('textbox', { name: /Motivo/ }), 'Imagem fora do padrão da marca')
    await user.click(within(dialog).getByRole('button', { name: 'Rejeitar anúncio' }))
    await waitFor(() => expect(fetchMock.mock.calls.some(([u]) => String(u).endsWith('/creatives/k1/review'))).toBe(true))
    const call = fetchMock.mock.calls.find(([u]) => String(u).endsWith('/creatives/k1/review'))!
    expect(JSON.parse((call[1] as RequestInit).body as string)).toEqual({ decision: 'REJECT', reason: 'Imagem fora do padrão da marca' })
    expect(headersOf(call[1] as RequestInit)['Content-Type']).toBe('application/json')
  })
})

describe('alteração pós-aprovação: resultado não some quando a campanha é recarregada', () => {
  function Harness({ campaign }: { campaign: CampaignDetail }) {
    const area = useCampaignArea()
    return <ChangeRequestDialog campaign={campaign} area={area} open onOpenChange={() => undefined} />
  }

  it.each([
    ['sem custo', null, 'Sem valor a pagar: a alteração foi para análise da RD.'],
    ['com custo', { order_id: 'o9', status: 'AGUARDANDO_PAGAMENTO', total_cents: 2500, expires_at: '2026-10-08T15:00:00Z' }, 'A diferença precisa ser paga antes da análise.'],
  ])('%s: após enviar, a recarga da campanha (WS/invalidação) mantém o resultado e a ação', async (_name, order, text) => {
    signInAs('advertiser')
    mockFetch((url, init) => {
      if (url.endsWith('/campaigns/c1/changes') && init.method === 'POST')
        return jsonResponse({
          campaign_id: 'c1',
          revision: 2,
          revision_id: 'r2',
          status: order ? 'AGUARDANDO_PAGAMENTO' : 'EM_ANALISE',
          changed_fields: ['time_bands'],
          previous_price_cents: 16071,
          new_price_cents: order ? 18571 : 14880,
          diff_cents: order ? 2500 : -1191,
          credit_due_cents: 0,
          quote: null,
          order,
          campaign_status: 'EM_ANALISE',
        })
      return jsonResponse({ items: [] })
    })
    const user = userEvent.setup()
    const campaign = campaignFixture({ id: 'c1', status: 'ATIVA' })
    const view = renderWithProviders(<Harness campaign={campaign} />, { route: '/anunciante/campanhas/c1' })
    await user.click(screen.getByRole('checkbox', { name: 'Alterar período e horários' }))
    await user.click(screen.getByRole('button', { name: 'Enviar alteração' }))
    expect(await screen.findByText('Revisão 2 criada')).toBeInTheDocument()
    // a campanha volta do servidor como NOVO objeto (mesmo diálogo aberto)
    view.rerender(<Harness campaign={{ ...campaign, status: 'EM_ANALISE', has_pending_change: true }} />)
    expect(screen.getByText('Revisão 2 criada')).toBeInTheDocument()
    expect(screen.getByText(text, { exact: false })).toBeInTheDocument()
    if (order) expect(screen.getByRole('button', { name: 'Pagar diferença' })).toBeInTheDocument()
  })
})

describe('estimativa de alteração = valor real (campaign_id na cotação)', () => {
  it('Simular preço envia campaign_id (sem org_id) e mostra o total do servidor', async () => {
    signInAs('advertiser')
    const fetchMock = mockFetch((url, init) => {
      if (url.endsWith('/pricing/quote') && init.method === 'POST')
        return jsonResponse({ quote: quoteFixture(14880), availability: availabilityFixture })
      return jsonResponse({ items: [] })
    })
    function Harness() {
      const area = useCampaignArea()
      return <ChangeRequestDialog campaign={campaignFixture({ id: 'c1', status: 'ATIVA' })} area={area} open onOpenChange={() => undefined} />
    }
    const user = userEvent.setup()
    renderWithProviders(<Harness />, { route: '/anunciante/campanhas/c1' })
    await user.click(screen.getByRole('checkbox', { name: 'Alterar período e horários' }))
    await user.click(screen.getByRole('button', { name: 'Simular preço' }))
    // o normalizador padr\u00e3o do Testing Library troca o espa\u00e7o n\u00e3o separ\u00e1vel do BRL por espa\u00e7o comum
    expect(await screen.findByText(/Estimativa do servidor para a campanha alterada: R\$ 148,80/)).toBeInTheDocument()
    const call = fetchMock.mock.calls.find(([u]) => String(u).endsWith('/pricing/quote'))!
    const body = JSON.parse(String((call[1] as RequestInit).body)) as Record<string, unknown>
    expect(body.campaign_id).toBe('c1')
    expect(body).not.toHaveProperty('org_id')
    expect(screen.queryByText(/sem os locais excluídos por conflito/)).not.toBeInTheDocument()
  })
})
