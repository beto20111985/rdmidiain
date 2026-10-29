import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'
import AdminDashboardPage from './AdminDashboardPage'
import EstablishmentDashboardPage from './EstablishmentDashboardPage'
import AdvertiserDashboardPage from './AdvertiserDashboardPage'
import { useAuthStore } from '@/stores/authStore'
import { useContextStore } from '@/stores/contextStore'
import type { AppContextInfo } from '@/features/auth/types'
import { makeMe, renderWithProviders } from '@/test/utils'
import { fetchRouter, page } from '@/components/finance/testing'

function signIn(permissions: string[], contexts: AppContextInfo[]) {
  const me = makeMe({ permissions, contexts })
  useAuthStore.setState({ status: 'authenticated', me, demo: false })
  useContextStore.getState().syncFromMe(contexts)
}

beforeEach(() => useContextStore.getState().reset())
afterEach(() => vi.unstubAllGlobals())

describe('Visão geral do ESTABELECIMENTO', () => {
  const ctx: AppContextInfo[] = [{ key: 'est:org1', label: 'Minha unidade', kind: 'ESTABLISHMENT', org_id: 'org1' }]
  const payload = {
    generated_at: '2026-10-07T20:30:00Z',
    org_id: 'org1',
    establishments: [{ id: 'est1', name: 'Mercado Teste', city: 'Goiânia', state: 'GO' }],
    screens: [
      { id: 's1', code: 'TEL-GO-7Q3M', name: 'Caixa 1', establishment_id: 'est1', status: 'ACTIVE', online: true, last_seen_at: '2026-10-07T20:29:00Z', sync: 'SINCRONIZADA' },
      { id: 's2', code: 'TEL-GO-8R4N', name: 'Entrada', establishment_id: 'est1', status: 'ACTIVE', online: false, last_seen_at: '2026-10-06T10:00:00Z', sync: 'SINCRONIZANDO' },
    ],
    kpis: { screens_total: 2, screens_online: 1, screens_offline: 1, exhibitions_month: 30, external_exhibitions_month: 18, internal_exhibitions_month: 12, exhibitions_period: 40, external_exhibitions_period: 25, internal_exhibitions_period: 15 },
    programming: { external_ads_running_today: 3, internal_contents_today: 2, note: 'Quantidades apenas — sem identificação de anunciantes (política comercial).' },
    commission: {
      pending_cents: 2500,
      available_cents: 1999,
      accumulated_cents: 10000,
      commission_month_cents: 999,
      commission_period_cents: 1234,
      valid_plays_period: 90,
      expected_plays_period: 100,
      valid_plays_month: 90,
      expected_plays_month: 100,
      performance_bps: 9000,
      estimated_lost_cents: 137,
      estimated_lost_note: 'Estimativa: exibições contratadas que não ocorreram × valor médio.',
      withdrawals: 'Saques ficam em Financeiro → Saques (não no painel).',
    },
    // Campos que NÃO devem aparecer mesmo que o servidor os envie por engano:
    advertiser_name: 'Anunciante Secreto SA',
    campaigns: [{ name: 'Campanha Secreta', advertiser: 'Anunciante Secreto SA' }],
  }

  it('sem dados comerciais do anunciante e SEM botão de saque (só o caminho Financeiro → Saques)', async () => {
    signIn(['dashboard.view', 'withdrawals.view', 'withdrawals.request', 'commissions.view', 'establishments.view'], ctx)
    fetchRouter([
      { match: /\/dashboards\/establishment\?org_id=org1/, reply: () => payload },
      { match: /\/establishments\?/, reply: () => page([{ id: 'est1', org_id: 'org1', name: 'Mercado Teste', timezone: 'America/Sao_Paulo' }]) },
    ])
    renderWithProviders(<EstablishmentDashboardPage />)
    expect(await screen.findByText('Caixa 1')).toBeInTheDocument()
    expect(screen.queryByText(/Anunciante Secreto/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Campanha Secreta/)).not.toBeInTheDocument()
    // nenhum botão de saque na visão geral
    expect(screen.queryByRole('button', { name: /saque|sacar/i })).not.toBeInTheDocument()
    const link = screen.getByRole('link', { name: /Saques ficam em Financeiro → Saques/ })
    expect(link).toHaveAttribute('href', '/unidade/financeiro/saques')
    // valores em BRL a partir de centavos
    expect(screen.getByText('R$ 19,99')).toBeInTheDocument()
    expect(screen.getByText('R$ 12,34')).toBeInTheDocument()
    expect(screen.getByText(/Você deixou de ganhar cerca de R\$ 1,37 neste período/)).toBeInTheDocument()
    // tela offline informa último contato
    expect(screen.getByText(/Sem comunicação\. Último contato:/)).toBeInTheDocument()
    // programação por quantidade apenas
    expect(screen.getByText('Anúncios externos em exibição')).toBeInTheDocument()
  })
})

describe('Visão geral do ANUNCIANTE', () => {
  const ctx: AppContextInfo[] = [{ key: 'adv:org2', label: 'Minhas campanhas', kind: 'ADVERTISER', org_id: 'org2' }]
  it('mostra somente região (UF) e quantidade de telas — nunca nome/endereço de estabelecimento', async () => {
    signIn(['dashboard.view', 'campaigns.view', 'campaigns.manage', 'payments.view', 'reports.view'], ctx)
    fetchRouter([
      {
        match: /\/dashboards\/advertiser\?org_id=org2/,
        reply: () => ({
          generated_at: '2026-10-07T20:30:00Z',
          org_id: 'org2',
          kpis: { campaigns_active: 1, campaigns_awaiting_approval: 0, campaigns_total: 1, creatives_total: 2 },
          campaigns: [{ id: 'c1', name: 'Campanha Alfa', status: 'ATIVA', start_date: '2026-10-01', end_date: '2026-10-31', contracted_screens: 12, cost_cents: 150000, approval: { approved_at: '2026-09-30T12:00:00Z', rejection_reason: null } }],
          creatives: [{ code: 'RD-ABCD23', name: 'Peça 1', status: 'APROVADO' }],
          invoices: [],
          proofs: [],
          regions_exhibited: [{ uf: 'GO', screens: 2 }],
          notes: ['Telas exibidas apenas por região (UF); nomes e endereços de estabelecimentos não são divulgados.'],
          // Vazamento simulado que a página NÃO pode exibir:
          establishments: [{ name: 'Supermercado Beta', address: 'Rua Secreta, 10' }],
        }),
      },
    ])
    renderWithProviders(<AdvertiserDashboardPage />)
    expect((await screen.findAllByText('Campanha Alfa')).length).toBeGreaterThan(0)
    expect(screen.getByText('GO')).toBeInTheDocument()
    expect(screen.getByText('2 telas')).toBeInTheDocument()
    expect(screen.getByText('12')).toBeInTheDocument() // telas contratadas (quantidade)
    expect(screen.getByText('R$ 1.500,00')).toBeInTheDocument()
    expect(screen.queryByText(/Supermercado Beta/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Rua Secreta/)).not.toBeInTheDocument()
    expect(screen.queryByText(/audiência/i)).not.toBeInTheDocument()
  })
})

describe('Visão geral RD', () => {
  it('KPIs reais, receita identificada (reconhecida × recebida), seletor de período e "Atualizado às"', async () => {
    signIn(['dashboard.view', 'finance.view', 'reports.view', 'campaigns.view', 'screens.view'], makeMe().contexts)
    fetchRouter([
      {
        match: /\/dashboards\/admin\?start=\d{4}-\d{2}-\d{2}&end=\d{4}-\d{2}-\d{2}$/,
        reply: () => ({
          generated_at: '2026-10-07T20:30:00Z',
          scope: { cities: null },
          kpis: { screens_online: 7, screens_total: 9, players_paired: 9, players_offline: 2, campaigns_active: 4, exhibitions_today: 77, exhibitions_month: 45678, exhibitions_period: 1234, advertisers_active: 3, advertisers_exhibited_period: 2 },
          operations: { screens_outdated: 1, campaigns_awaiting_approval: 2, media_errors_30d: 0, screens_offline_long: 1 },
          alerts: [{ type: 'campanhas_aguardando_aprovacao', label: 'Campanhas aguardando aprovação', count: 2 }],
          distribution_by_state: [{ uf: 'GO', screens: 9, online: 7 }],
          capacity_url: '/api/v1/dashboards/admin/capacity',
          period: { start: '2026-09-08', end: '2026-10-07', timezone: 'America/Sao_Paulo', explicit: true, note: 'Indicadores de ESTADO são do momento atual; indicadores de FLUXO usam o período.' },
          finance: { revenue_month_cents: 400000, billing_month_cents: 600000, revenue_period_cents: 500000, billing_period_cents: 700000, pending_receivables_cents: 120000, commission_pending_cents: 25000, revenue_projection_cents: 1550000, projection_method: 'Linear: receita reconhecida em 7 de 31 dias do mês.' },
        }),
      },
      { match: /\/dashboards\/admin\/capacity/, reply: () => ({ date: '2026-10-07', screens: 9, free_units: 1240, unit: 'SLOT_30S', unit_seconds: 30, free_seconds: 37200, truncated: false, note: 'Tempo livre para novas inserções hoje.' }) },
      { match: /\/reports\/data\/exibicoes_por_dia/, reply: () => ({ title: 'Exibições por dia', audience: 'EQUIPE', columns: [], rows: [{ day: '2026-10-06', valid: 100 }, { day: '2026-10-07', valid: 50 }], meta: [], notes: [], total: 2 }) },
      { match: /\/campaigns\?/, reply: () => page([]) },
    ])
    renderWithProviders(<AdminDashboardPage />)
    expect(await screen.findByText('Telas online')).toBeInTheDocument()
    expect(screen.getByText('Exibições registradas no período')).toBeInTheDocument()
    expect(screen.getByText('1.234')).toBeInTheDocument() // exibições do período (start/end enviados ao servidor)
    expect(screen.getByText(/Indicadores de ESTADO são do momento atual/)).toBeInTheDocument()
    expect(screen.getByText('Receita reconhecida')).toBeInTheDocument()
    expect(screen.getByText('Faturamento recebido')).toBeInTheDocument()
    expect(screen.getByText('R$ 5.000,00')).toBeInTheDocument()
    expect(screen.getByText(/Linear: receita reconhecida em 7 de 31 dias/)).toBeInTheDocument()
    expect(screen.getByText(/Atualizado às/)).toBeInTheDocument()
    for (const p of ['Hoje', '7 dias', '30 dias', 'Personalizado']) expect(screen.getByRole('radio', { name: p })).toBeInTheDocument()
    expect(await screen.findByText(/150 exibições válidas no período/)).toBeInTheDocument()
    expect(await screen.findByText('1.240')).toBeInTheDocument() // capacidade livre
    expect(screen.getByText('unidades de 30 s livres')).toBeInTheDocument()
    expect(screen.getByText(/10 h 20 min livres hoje em 9 telas vendáveis/)).toBeInTheDocument()
    expect(screen.queryByText(/ocupa/i)).not.toBeInTheDocument() // E2 #717: sem ocupação do inventário
  })
})
