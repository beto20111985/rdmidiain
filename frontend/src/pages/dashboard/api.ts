/**
 * Dashboards agregados por perfil (W-BIZ `/dashboards`). Sem polling: o RealtimeProvider
 * invalida o domínio 'dashboard' nos eventos `screen.status`, `campaign.updated`,
 * `payment.updated` e `approval.pending`.
 */
import { http } from '@/lib/http'
import type { PaymentOrder } from '@/pages/finance/api'

/** Período aplicado pelo servidor (dias locais America/Sao_Paulo). */
export interface DashboardPeriod {
  start: string
  end: string
  timezone: string
  explicit: boolean
  note: string
}

export interface PeriodQuery {
  start?: string
  end?: string
}

export interface AdminDashboard {
  generated_at: string
  scope: { cities: string[] | null }
  period?: DashboardPeriod
  kpis: {
    screens_online: number
    screens_total: number
    players_paired: number
    players_offline: number
    campaigns_active: number
    exhibitions_today: number
    exhibitions_month: number
    exhibitions_period?: number
    advertisers_active: number
    advertisers_exhibited_period?: number
  }
  operations: {
    screens_outdated: number
    campaigns_awaiting_approval: number
    media_errors_30d: number
    media_errors_period?: number
    screens_offline_long: number
  }
  alerts: Array<{ type: string; label: string; count: number }>
  distribution_by_state: Array<{ uf: string | null; screens: number; online: number; exhibitions_period?: number }>
  capacity_url: string
  finance: null | {
    revenue_month_cents: number
    billing_month_cents: number
    revenue_period_cents?: number
    billing_period_cents?: number
    pending_receivables_cents: number
    commission_pending_cents: number
    revenue_projection_cents: number | null
    projection_method: string
  }
}

/** Capacidade DISPONÍVEL (livre) — sem ocupação nem capacidade total/usada (E2 #717 NÃO; #718 SIM). */
export interface AdminCapacity {
  date: string
  screens: number
  free_units: number
  unit: string
  unit_seconds: number
  free_seconds: number
  truncated: boolean
  note?: string
}

export interface EstablishmentDashboard {
  generated_at: string
  org_id: string
  period?: DashboardPeriod
  establishments: Array<{ id: string; name: string; city: string | null; state: string | null }>
  screens: Array<{
    id: string
    code: string
    name: string
    establishment_id: string
    status: string
    online: boolean
    last_seen_at: string | null
    sync: 'SINCRONIZANDO' | 'SINCRONIZADA' | 'NUNCA' | null
  }>
  kpis: {
    screens_total: number
    screens_online: number
    screens_offline: number
    exhibitions_month: number
    external_exhibitions_month: number
    internal_exhibitions_month: number
    exhibitions_period?: number
    external_exhibitions_period?: number
    internal_exhibitions_period?: number
  }
  programming: { external_ads_running_today: number; internal_contents_today: number; note: string }
  commission: null | {
    pending_cents: number
    available_cents: number
    accumulated_cents: number
    commission_month_cents: number
    valid_plays_month: number
    expected_plays_month: number
    commission_period_cents?: number
    valid_plays_period?: number
    expected_plays_period?: number
    performance_bps: number | null
    estimated_lost_cents: number | null
    estimated_lost_note: string | null
    withdrawals: string
  }
}

export interface AdvertiserDashboard {
  generated_at: string
  org_id: string
  period?: DashboardPeriod
  kpis: { campaigns_active: number; campaigns_awaiting_approval: number; campaigns_total: number; creatives_total: number }
  campaigns: Array<{
    id: string
    name: string
    status: string
    start_date: string | null
    end_date: string | null
    contracted_screens: number | null
    cost_cents: number
    approval: { approved_at: string | null; rejection_reason: string | null }
  }>
  creatives: Array<{ code: string; name: string; status: string }>
  invoices: PaymentOrder[]
  proofs: Array<{
    id: string
    code: string
    campaign_id: string
    period_start: string
    period_end: string
    total_valid_plays: number
    pdf_url: string
  }>
  regions_exhibited: Array<{ uf: string | null; screens: number }>
  notes: string[]
}

export interface CampaignLite {
  id: string
  org_id: string
  name: string
  status: string
  start_date: string | null
  end_date: string | null
  contracted_price_cents: number | null
}

export const dashboardApi = {
  admin: (p: PeriodQuery = {}) => http.get<AdminDashboard>('/dashboards/admin', { query: { ...p } }),
  capacity: () => http.get<AdminCapacity>('/dashboards/admin/capacity'),
  establishment: (orgId: string, p: PeriodQuery = {}) =>
    http.get<EstablishmentDashboard>('/dashboards/establishment', { query: { org_id: orgId, ...p } }),
  advertiser: (orgId: string, p: PeriodQuery = {}) =>
    http.get<AdvertiserDashboard>('/dashboards/advertiser', { query: { org_id: orgId, ...p } }),
  endingCampaigns: () =>
    http.get<{ items: CampaignLite[]; total: number }>('/campaigns', { query: { status: 'ATIVA', sort: 'end_date', page_size: 5 } }),
}
