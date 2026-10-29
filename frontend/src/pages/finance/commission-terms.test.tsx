import { afterEach, describe, expect, it, vi } from 'vitest'
import { screen, within } from '@testing-library/react'
import { CommissionTermsSheet } from './commission-terms'
import { jsonResponse, renderWithProviders } from '@/test/utils'
import { fetchRouter } from '@/components/finance/testing'
import type { CampaignCommissionTerms } from './api'

const nbsp = (s: string) => s.replace(new RegExp(String.fromCharCode(160), 'g'), ' ')

const FULL: CampaignCommissionTerms = {
  campaign_id: 'c1',
  visibility: 'FULL',
  terms: [
    {
      terms_id: 't1', kind: 'INITIAL', payment_order_id: 'po-111111-aaaa', model: 'A_POOL', pool_bps: 500,
      eligible_revenue_cents: 100000, pool_cents: 5000, released_cents: 5000,
      period_start: '2026-09-01', period_end: '2026-09-30', status: 'CANCELLED', cancelled_on: '2026-09-20',
      expected_exhibitions: 1000, valid_plays: 900, commission_cents: 4500, commission_ucents: 4500000000, clawback_cents: 300,
      registered_at: '2026-09-01T12:00:00Z',
    },
    {
      terms_id: 't2', kind: 'RENEWAL', payment_order_id: 'po-222222-bbbb', model: 'A_POOL', pool_bps: 500,
      eligible_revenue_cents: 100000, pool_cents: 5000, released_cents: 0,
      period_start: '2026-10-01', period_end: '2026-10-31', status: 'ACTIVE', cancelled_on: null,
      expected_exhibitions: null, valid_plays: 10, commission_cents: 50, commission_ucents: 50000000, clawback_cents: 0,
      registered_at: '2026-10-01T12:00:00Z',
    },
  ],
  totals: { eligible_revenue_cents: 200000, pool_cents: 10000, released_cents: 5000, commission_cents: 4550, clawback_cents: 300, valid_plays: 910 },
}

const EST: CampaignCommissionTerms = {
  campaign_id: 'c1',
  visibility: 'ESTABLISHMENT',
  terms: [
    { terms_id: 't3', kind: 'CHANGE', period_start: '2026-10-01', period_end: '2026-10-31', status: 'ACTIVE', cancelled_on: null, valid_plays: 40, commission_cents: 120, commission_ucents: 120000000, clawback_cents: 0 },
  ],
  totals: { commission_cents: 120, clawback_cents: 0, valid_plays: 40 },
}

afterEach(() => vi.unstubAllGlobals())

describe('Termos de comissão por pagamento (W-FIN 0006)', () => {
  it('visão FULL: tipos em pt-BR, cobrança, pool, liberado, estorno e cancelamento', async () => {
    const r = fetchRouter([{ match: /\/financial\/commissions\/campaigns\/c1$/, reply: () => FULL }])
    renderWithProviders(<CommissionTermsSheet target={{ campaignId: 'c1', label: 'Campanha X' }} onOpenChange={vi.fn()} />)
    const list = await screen.findByRole('list', { name: 'Termos por pagamento' })
    expect(within(list).getByText(/Contratação inicial · 01\/09\/2026 a 30\/09\/2026/)).toBeInTheDocument()
    expect(within(list).getByText(/Renovação · 01\/10\/2026 a 31\/10\/2026/)).toBeInTheDocument()
    expect(within(list).getByText('Cancelado')).toBeInTheDocument()
    expect(within(list).getByText('Vigente')).toBeInTheDocument()
    expect(within(list).getByText(/Cancelado em 20\/09\/2026/)).toBeInTheDocument()
    expect(within(list).getByText('po-11111')).toBeInTheDocument()
    expect(within(list).getAllByText('A — pool fixo')).toHaveLength(2)
    expect(within(list).getByText('900 / 1.000')).toBeInTheDocument()
    expect(within(list).getByText('10 / após a 1ª apropriação')).toBeInTheDocument()
    expect(screen.getAllByText('Receita elegível').length).toBeGreaterThan(0)
    expect(nbsp(screen.getByRole('region', { name: 'Totais da campanha' }).textContent ?? '')).toContain('R$ 45,50')
    expect(r.find('GET', /commissions\/campaigns\/c1/)).toHaveLength(1)
  })

  it('visão ESTABLISHMENT: só a comissão da unidade, sem cobrança/receita/pool/modelo', async () => {
    fetchRouter([{ match: /\/financial\/commissions\/campaigns\/c1$/, reply: () => EST }])
    renderWithProviders(<CommissionTermsSheet target={{ campaignId: 'c1', label: 'Contratação 1' }} onOpenChange={vi.fn()} />)
    const list = await screen.findByRole('list', { name: 'Termos por pagamento' })
    expect(within(list).getByText(/Alteração de contrato · 01\/10\/2026 a 31\/10\/2026/)).toBeInTheDocument()
    expect(screen.getAllByText('Sua comissão').length).toBeGreaterThan(0)
    for (const forbidden of ['Cobrança', 'Receita elegível', 'Pool', 'Liberado', 'Modelo', 'Liberado nos fechamentos']) {
      expect(screen.queryByText(forbidden)).not.toBeInTheDocument()
    }
    expect(screen.getByText(/Termos de comissão — Contratação 1/)).toBeInTheDocument()
  })

  it('404 (fora do escopo ou sem termos) vira estado vazio, sem revelar a campanha', async () => {
    fetchRouter([{ match: /\/financial\/commissions\/campaigns\/c9$/, reply: () => jsonResponse({ error: { code: 'NOT_FOUND', message: 'Não encontrado.' } }, 404) }])
    renderWithProviders(<CommissionTermsSheet target={{ campaignId: 'c9', label: 'Contratação 2' }} onOpenChange={vi.fn()} />)
    expect(await screen.findByText('Sem termos de comissão para mostrar.')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})
