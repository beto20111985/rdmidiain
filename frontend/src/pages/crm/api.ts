/**
 * CRM / Comercial (W-BIZ `/crm`): leads e funil, atividades, propostas (desconto com limite e
 * aprovação, PDF, validade de 30 dias a partir do ENVIO, conversão em pedido pelo checkout
 * real do W-CAMP), estrutura comercial e atribuições (comissão do vendedor).
 */
import { http, type Paginated } from '@/lib/http'

export interface Lead {
  id: string
  name: string
  company: string | null
  email: string | null
  phone: string | null
  source: string
  interest: 'ANUNCIANTE' | 'PARCEIRO' | 'AMBOS' | null
  stage: string
  owner_id: string | null
  /** Nome resolvido pelo servidor (somente o nome). */
  owner_name?: string | null
  city: string | null
  uf: string | null
  city_key: string | null
  org_id: string | null
  org_name?: string | null
  estimated_value_cents: number | null
  lost_reason: string | null
  notes: string | null
  stage_changed_at: string | null
  created_at: string | null
  updated_at: string | null
}

export interface LeadDetail extends Lead {
  activities: Array<{ id: string; kind: string; description: string; occurred_at: string | null; actor_id: string | null; actor_name?: string | null }>
  stage_history: Array<{ from: string | null; to: string; reason: string | null; at: string | null; actor_id: string | null; actor_name?: string | null }>
  proposals: Array<{ id: string; number: string; status: string; total_cents: number }>
}

export interface Pipeline {
  stages: Array<{ stage: string; count: number; estimated_value_cents: number }>
  total: number
  conversion_rate_bps: number | null
}

export interface ProposalItem {
  id?: string
  position?: number
  package_code: string | null
  description: string
  quantity: number
  unit_price_cents: number
  line_total_cents?: number
  details?: Record<string, unknown>
}

export interface Proposal {
  id: string
  number: string
  title: string
  status: string
  stored_status: string
  lead_id: string | null
  org_id: string | null
  org_name?: string | null
  seller_id: string
  seller_name?: string | null
  city_key: string | null
  subtotal_cents: number
  discount_bps: number
  discount_cents: number
  total_cents: number
  discount_limit_bps: number | null
  discount_approval_required: boolean
  discount_justification: string | null
  discount_approved_by: string | null
  discount_approved_by_name?: string | null
  discount_approved_at: string | null
  discount_decision_reason: string | null
  validity_days: number
  valid_until: string | null
  expires_at: string | null
  sent_at: string | null
  accepted_at: string | null
  converted_at: string | null
  order_id: string | null
  pdf_sha256: string | null
  notes: string | null
  version: number
  created_at: string | null
  updated_at: string | null
  items?: ProposalItem[]
  emailed?: boolean
}

export interface ConvertResult extends Proposal {
  order_total_cents: number
  proposal_total_cents: number
  checkout: null | {
    order_id: string
    status: string
    kind?: string
    media_cents: number
    fees_cents: number
    discount_cents: number
    total_cents: number
    expires_at: string | null
    coupon_code: string | null
    items?: Array<Record<string, unknown>>
    payment: null | { id?: string; status?: string; amount_cents?: number; pix_qr_code?: string | null; expires_at?: string | null }
    warnings: string[]
    error?: { code: string; message: string; details?: Record<string, unknown> }
  }
}

export interface SalesRep {
  id: string
  user_id: string
  user_name?: string | null
  kind: string
  region_label: string | null
  city_key: string | null
  uf: string | null
  manager_id: string | null
  manager_name?: string | null
  compensation: string
  commission_bps: number | null
  is_active: boolean
}

export interface Attribution {
  id: string
  proposal_id: string
  order_id: string
  seller_id: string
  seller_name?: string | null
  org_id: string
  org_name?: string | null
  amount_cents: number
  commission_bps: number
  commission_cents: number
  status: string
  created_at: string | null
}

type Q = Record<string, string | number | boolean | undefined | null>

/** Cliente elegível de proposta (W-BIZ `client-options`): anunciantes/agências ativos, nunca a própria instância. */
export interface ClientOption {
  id: string
  name: string
  trade_name: string | null
  city: string | null
  uf: string | null
  business_roles: string[]
}

/** Campanha do cliente elegível para a conversão (W-BIZ `convertible-campaigns`). */
export interface ConvertibleCampaign {
  id: string
  name: string
  status: string
  start_date: string | null
  end_date: string | null
  duration_s: number | null
  ready: boolean
  issues: Array<{ code: string; message: string }>
}

export const crmApi = {
  leads: (q: Q) => http.get<Paginated<Lead>>('/crm/leads', { query: q }),
  lead: (id: string) => http.get<LeadDetail>(`/crm/leads/${id}`),
  createLead: (body: Record<string, unknown>) => http.post<Lead>('/crm/leads', body),
  patchLead: (id: string, body: Record<string, unknown>) => http.patch<Lead>(`/crm/leads/${id}`, body),
  changeStage: (id: string, stage: string, reason?: string) => http.post<Lead>(`/crm/leads/${id}/stage`, { stage, reason: reason || undefined }),
  addActivity: (id: string, body: { kind: string; description: string; occurred_at?: string }) =>
    http.post<{ id: string }>(`/crm/leads/${id}/activities`, body),
  pipeline: () => http.get<Pipeline>('/crm/pipeline'),
  clientOptions: (search: string, limit = 20) =>
    http.get<{ items: ClientOption[] }>('/crm/client-options', { query: { search: search || undefined, limit } }),

  proposals: (q: Q) => http.get<Paginated<Proposal>>('/crm/proposals', { query: q }),
  proposal: (id: string) => http.get<Proposal>(`/crm/proposals/${id}`),
  createProposal: (body: Record<string, unknown>) => http.post<Proposal>('/crm/proposals', body),
  patchProposal: (id: string, body: Record<string, unknown>) => http.patch<Proposal>(`/crm/proposals/${id}`, body),
  submit: (id: string) => http.post<Proposal>(`/crm/proposals/${id}/submit`),
  discountDecision: (id: string, approve: boolean, reason?: string) =>
    http.post<Proposal>(`/crm/proposals/${id}/discount-decision`, { approve, reason: reason || undefined }),
  send: (id: string, email_to?: string) => http.post<Proposal>(`/crm/proposals/${id}/send`, { email_to: email_to || undefined }),
  clientDecision: (id: string, accepted: boolean, note?: string) =>
    http.post<Proposal>(`/crm/proposals/${id}/client-decision`, { accepted, note: note || undefined }),
  cancel: (id: string, reason: string) => http.post<Proposal>(`/crm/proposals/${id}/cancel`, { reason }),
  convertibleCampaigns: (id: string) => http.get<{ items: ConvertibleCampaign[] }>(`/crm/proposals/${id}/convertible-campaigns`),
  convert: (id: string, body: { campaign_ids: string[]; coupon_code?: string }, idempotencyKey: string) =>
    http.post<ConvertResult>(`/crm/proposals/${id}/convert`, body, { idempotencyKey }),

  salesReps: () => http.get<{ items: SalesRep[] }>('/crm/sales-reps'),
  upsertRep: (body: Record<string, unknown>) => http.put<SalesRep>('/crm/sales-reps', body),
  attributions: (q: Q) => http.get<Paginated<Attribution>>('/crm/attributions', { query: q }),
}
