/**
 * API financeira (W-FIN) — prefixo /api/v1/financial. Tipos espelham os `*_public` do backend
 * (app/modules/financial/*). Valores SEMPRE em centavos (`*_cents`), datas UTC ISO (`Z`) e
 * datas de competência `YYYY-MM-DD`. Operações sensíveis usam `Idempotency-Key`.
 */
import { http, newRequestId, type Paginated } from '@/lib/http'
import type { ApprovalModeCode } from '@/features/auth/types'

// ─── Tipos ───────────────────────────────────────────────────────────────────

export interface PaymentOrder {
  id: string
  org_id: string
  reference_type: string | null
  reference_id: string | null
  items: Array<Record<string, unknown>> | null
  amount_cents: number
  eligible_revenue_cents: number | null
  services_cents: number | null
  currency: string
  method: string | null
  installments: number | null
  status: string
  dispute_status: string | null
  provider: string | null
  provider_external_ref: string | null
  provider_transaction_id: string | null
  pix_qr_code: string | null
  pix_end_to_end_id: string | null
  paid_amount_cents: number | null
  fee_cents: number | null
  net_cents: number | null
  refunded_cents: number | null
  expires_at: string | null
  paid_at: string | null
  created_at: string
  provider_mode_fake: boolean
}

export interface ReceivablesPage extends Paginated<PaymentOrder> {
  pending_total_cents: number
}

export interface FinanceOverview {
  start: string
  end: string
  received_cents: number
  pending_receivables_cents: number
  refunded_cents: number
  gateway_fees_cents: number
  commission_expense_cents: number
  commission_payable_cents: number
  withdrawals_paid_cents: number
  cash_cloudpaypag_cents: number
  accounts_payable_cents: number
}

export interface Payable {
  id: string
  description: string
  category: string
  supplier_name: string | null
  amount_cents: number
  competence_date: string
  due_date: string
  status: string
  settled_at: string | null
  overdue: boolean
  approval_request_id: string
}

export interface CashFlowRow {
  period: string
  inflow_cents: number
  outflow_cents: number
  net_cents: number
  balance_cents: number
}
export interface CashFlow {
  start: string
  end: string
  opening_balance_cents: number
  rows: CashFlowRow[]
  closing_balance_cents: number
}

export interface DreLine {
  group: string
  label: string
  amount_cents: number
}
export interface Dre {
  start: string
  end: string
  lines: DreLine[]
  gross_revenue_cents: number
  net_revenue_cents: number
  result_cents: number
  margin_bps: number | null
}

export interface Closing {
  id: string
  period_start: string
  period_end: string
  closed_at: string
  closed_by: string | null
  snapshot_sha256: string
  transaction_count: number
  snapshot?: Record<string, unknown>
}

export interface TrialBalanceRow {
  id?: string
  account_id?: string
  code: string
  name?: string
  kind?: string
  owner_id?: string | null
  org_id?: string | null
  balance_cents?: number
  raw_cents: number
  [k: string]: unknown
}
export interface AccountsResponse extends Paginated<TrialBalanceRow> {
  check_sum_cents: number
}

export interface StatementItem {
  posting_id: string
  transaction_id: string
  kind: string
  description: string
  competence_date: string
  occurred_at: string
  account_code: string
  account_name: string
  owner_id: string | null
  amount_cents: number
  natural_amount_cents: number
  memo: string | null
  reverses_transaction_id: string | null
}
export interface StatementResponse extends Paginated<StatementItem> {
  balances: Record<string, number>
}

export interface ReconRun {
  id: string
  kind: string
  status: string
  provider_balance_cents: number | null
  internal_cash_cents: number | null
  summary: Record<string, unknown> | null
  started_at: string
  finished_at: string | null
}

export interface ReconItem {
  id: string
  run_id: string | null
  kind: string
  status: 'DIVERGENT' | 'RESOLVED' | string
  internal_ref: string | null
  external_ref: string | null
  internal_amount_cents: number | null
  external_amount_cents: number | null
  detail: Record<string, unknown> | null
  created_at: string
  resolved_at: string | null
  resolution_approval_id: string | null
  resolution_transaction_id: string | null
}

export interface ApprovalRequest {
  id: string
  action_type: string
  action_label: string
  payload: Record<string, unknown>
  payload_sha256: string
  amount_cents: number | null
  reason: string
  requested_by: string
  /** Somente o nome (entre Admin Master o outro aparece apenas por nome — P001 §84). */
  requested_by_name?: string | null
  requested_at: string
  status: string
  approved_by: string | null
  approved_by_name?: string | null
  approved_at: string | null
  rejected_by: string | null
  rejected_by_name?: string | null
  rejection_reason: string | null
  expires_at: string
  execution_state: string
  execution_ref: string | null
  execution_error: string | null
  executed_at: string | null
  requires_email_code: boolean
  /** DUAL: outro Master aprova (viewer ≠ solicitante, PENDING). */
  can_approve: boolean
  // --- AÇÃO 003 (SPEC §4.2 request_public). Opcionais no tipo só por compatibilidade de fixtures antigas. ---
  /** Modo da instância no momento da criação (snapshot). Ausente = DUAL (comportamento anterior). */
  approval_mode?: ApprovalModeCode
  /** SINGLE: o solicitante autoriza o próprio pedido (rota /authorize). */
  self_authorized_by?: string | null
  self_authorized_by_name?: string | null
  self_authorized_at?: string | null
  /** O pedido muda o modo de aprovação financeira da instância. */
  is_mode_change?: boolean
  /** SINGLE, solicitante = viewer, PENDING. */
  can_authorize?: boolean
}

export interface ApprovalEvent {
  id: string
  event: string
  actor_user_id: string | null
  data: Record<string, unknown> | null
  created_at: string
}

export interface ApprovalActionInfo {
  action_type: string
  label: string
  pix_out: boolean
}

/** Fatores de uma confirmação reforçada na CRIAÇÃO (mudança de modo DUAL → ÚNICA exige TOTP + senha financeira). */
export interface StepUpInput {
  totp_code: string
  financial_password: string
}

export interface Withdrawal {
  id: string
  kind: string
  origin: string
  establishment_id: string | null
  amount_cents: number
  sent_cents: number | null
  fee_cents: number | null
  net_cents: number | null
  status: string
  destination_masked: string | null
  pix_key_type: string | null
  provider_external_id: string | null
  provider_status: string | null
  failure_reason: string | null
  approval_request_id: string | null
  requested_at: string
  completed_at: string | null
}

export interface WithdrawalSimulation {
  amount_cents: number
  fee_cents: number | null
  net_cents: number | null
  available_cents: number
  sufficient: boolean
  blocked_negative_balance: boolean
  provider_operational: boolean
  destination_masked: string
}

export interface PixKey {
  id: string
  establishment_id: string
  key_type: string
  key_masked: string
  holder_name: string
  holder_document_masked: string
  active_from: string
  created_at: string
  revoked_at: string | null
  status: 'ACTIVE' | 'COOLDOWN' | 'REVOKED' | string
}

export interface EstablishmentBalance {
  establishment_id: string
  pending_provisional_cents: number
  pending_closed_cents: number
  available_cents: number
  in_transit_cents: number
  negative_balance_cents: number
  withdrawals_blocked: boolean
  paid_cents: number
  accumulated_cents: number
}

export interface CommissionRow {
  campaign_id?: string
  screen_id?: string
  local_day?: string
  competence?: string
  establishment_id: string
  valid_plays: number
  expected_plays: number
  amount_ucents: number
  amount_cents: number
  status: 'PENDENTE' | 'FECHADO' | string
}

export type CommissionGroupBy = 'campaign' | 'screen' | 'day' | 'month' | 'campaign_screen_day'

/** Termo de comissão POR PAGAMENTO (W-FIN 0006): contratação inicial, renovação ou alteração. */
export type CommissionTermKind = 'INITIAL' | 'RENEWAL' | 'CHANGE'

interface CommissionTermBase {
  terms_id: string
  kind: CommissionTermKind | string
  period_start: string
  period_end: string
  status: 'ACTIVE' | 'CANCELLED' | string
  cancelled_on: string | null
  valid_plays: number
  commission_cents: number
  commission_ucents: number
  clawback_cents: number
}

/** Visão completa (escopo GLOBAL: financeiro/super admin). */
export interface CommissionTermFull extends CommissionTermBase {
  payment_order_id: string | null
  model: 'A_POOL' | 'B_ATTRIBUTED' | string
  pool_bps: number
  eligible_revenue_cents: number
  pool_cents: number
  released_cents: number
  expected_exhibitions: number | null
  registered_at: string | null
}

export type CampaignCommissionTerms =
  | {
      campaign_id?: string
      visibility: 'FULL'
      terms: CommissionTermFull[]
      totals: { eligible_revenue_cents: number; pool_cents: number; released_cents: number; commission_cents: number; clawback_cents: number; valid_plays: number }
    }
  | {
      campaign_id?: string
      /** Estabelecimento: só os termos de que participa e só a própria comissão (sem dados comerciais). */
      visibility: 'ESTABLISHMENT'
      terms: CommissionTermBase[]
      totals: { commission_cents: number; clawback_cents: number; valid_plays: number }
    }

export interface CommissionExample {
  pool_cents: number
  per_screen_cents: number[]
  total_cents: number
  rd_residue_cents: number
}
export interface CommissionModels {
  current_model: string | null
  pool_bps: number
  base_revenue_cents: number
  models: Record<string, { name: string; description: string }>
  rules: string[]
  examples: Array<{ scenario: string; model_a: CommissionExample; model_b: CommissionExample }>
}

export interface CommissionSettings {
  establishment_id: string
  instance_model: string
  instance_pool_bps: number
  effective: { receives_commission: boolean; weight_milli: number; rate_bps: number; auto_payout: boolean }
  history: Array<{
    id: string
    receives_commission: boolean
    rate_bps: number | null
    weight_milli: number | null
    auto_payout: boolean
    valid_from: string
    valid_to: string | null
    approved_by: string | null
    approval_request_id: string | null
  }>
  change_requires: string
}

export interface ProviderStatus {
  provider: string
  instance_slug: string | null
  mode: string
  base_url: string | null
  token_configured: boolean
  webhook_secret_configured: boolean
  subconta_id: string | null
  external_validation: string
  capabilities: Record<string, string>
  fake_warning: string | null
}

export interface SecurityStatus {
  totp_enabled: boolean
  financial_password_set: boolean
  financial_password_changed_at: string | null
}

export interface Refund {
  id: string
  payment_order_id: string
  campaign_id: string | null
  policy: string
  as_of: string
  refund_cents: number
  media_refund_cents: number
  services_refund_cents: number
  status: string
  reason: string
  calculation: Record<string, unknown> | null
  approval_request_id: string | null
  created_at: string
}

export interface FiscalDocumentResult {
  id: string
  status: string
  detail: string | null
}

// ─── Chamadas ────────────────────────────────────────────────────────────────
type Q = Record<string, string | number | boolean | undefined | null>

export const financeApi = {
  providerStatus: () => http.get<ProviderStatus>('/financial/provider/status'),
  securityStatus: () => http.get<SecurityStatus>('/financial/security/status'),

  overview: (start: string, end: string) => http.get<FinanceOverview>('/financial/overview', { query: { start, end } }),
  receivables: (q: Q) => http.get<ReceivablesPage>('/financial/receivables', { query: q }),
  invoices: (q: Q) => http.get<Paginated<PaymentOrder>>('/financial/invoices', { query: q }),
  invoice: (id: string) => http.get<PaymentOrder>(`/financial/invoices/${id}`),
  requestFiscalDocument: (id: string) => http.post<FiscalDocumentResult>(`/financial/invoices/${id}/fiscal-document`),

  payables: (q: Q) => http.get<Paginated<Payable>>('/financial/payables', { query: q }),
  createExpense: (body: Record<string, unknown>, idempotencyKey = newRequestId()) =>
    http.post<ApprovalRequest>('/financial/payables', body, { idempotencyKey }),
  updateExpense: (id: string, body: Record<string, unknown>) => http.patch<ApprovalRequest>(`/financial/payables/${id}`, body),
  settlePayable: (id: string, body: Record<string, unknown>) =>
    http.post<ApprovalRequest>(`/financial/payables/${id}/settle`, body),

  cashFlow: (start: string, end: string, granularity: 'day' | 'month') =>
    http.get<CashFlow>('/financial/cash-flow', { query: { start, end, granularity } }),
  dre: (start: string, end: string) => http.get<Dre>('/financial/dre', { query: { start, end } }),

  closings: () => http.get<Paginated<Closing>>('/financial/closings'),
  closing: (id: string) => http.get<Closing>(`/financial/closings/${id}`),
  closePeriod: (period: string) => http.post<Closing>('/financial/closings', { period }),

  accounts: (q: Q) => http.get<AccountsResponse>('/financial/accounts', { query: q }),
  accountStatement: (id: string, q: Q) => http.get<StatementResponse>(`/financial/accounts/${id}/statement`, { query: q }),
  orgStatement: (orgId: string, q: Q) => http.get<StatementResponse>(`/financial/organizations/${orgId}/statement`, { query: q }),

  reconRuns: (q: Q) => http.get<Paginated<ReconRun>>('/financial/reconciliation/runs', { query: q }),
  reconRunNow: () => http.post<ReconRun>('/financial/reconciliation/runs'),
  reconItems: (q: Q) => http.get<Paginated<ReconItem>>('/financial/reconciliation/items', { query: q }),
  reconResolve: (id: string, body: Record<string, unknown>) =>
    http.post<ApprovalRequest>(`/financial/reconciliation/items/${id}/resolution`, body),

  approvals: (q: Q) => http.get<Paginated<ApprovalRequest>>('/financial/approvals', { query: q }),
  approval: (id: string) => http.get<ApprovalRequest>(`/financial/approvals/${id}`),
  approvalEvents: (id: string) => http.get<{ items: ApprovalEvent[] }>(`/financial/approvals/${id}/events`),
  approvalActions: () => http.get<{ items: ApprovalActionInfo[]; approval_mode?: ApprovalModeCode }>('/financial/approvals/actions'),
  createApproval: (
    body: { action_type: string; payload: Record<string, unknown>; reason: string; step_up?: StepUpInput },
    idempotencyKey = newRequestId(),
  ) => http.post<ApprovalRequest>('/financial/approvals', body, { idempotencyKey }),
  approvalEmailCode: (id: string) => http.post<{ sent: boolean; expires_in: number }>(`/financial/approvals/${id}/email-code`),
  approve: (
    id: string,
    body: { totp_code: string; financial_password: string; payload_sha256: string; email_code?: string },
    idempotencyKey = newRequestId(),
  ) => http.post<ApprovalRequest>(`/financial/approvals/${id}/approve`, body, { idempotencyKey }),
  /** Aprovação ÚNICA: o próprio solicitante autoriza (TOTP + senha financeira + código por e-mail). */
  authorize: (
    id: string,
    body: { totp_code: string; financial_password: string; payload_sha256: string; email_code?: string },
    idempotencyKey = newRequestId(),
  ) => http.post<ApprovalRequest>(`/financial/approvals/${id}/authorize`, body, { idempotencyKey }),
  reject: (id: string, reason: string) => http.post<ApprovalRequest>(`/financial/approvals/${id}/reject`, { reason }),
  cancelApproval: (id: string) => http.post<ApprovalRequest>(`/financial/approvals/${id}/cancel`),

  withdrawals: (q: Q) => http.get<Paginated<Withdrawal>>('/financial/withdrawals', { query: q }),
  simulateWithdrawal: (establishment_id: string, amount_cents: number) =>
    http.post<WithdrawalSimulation>('/financial/withdrawals/simulate', { establishment_id, amount_cents }),
  /** Código por e-mail do saque: vale só para ESTE estabelecimento e ESTE valor (10 min, uso único). */
  withdrawalEmailCode: (establishment_id: string, amount_cents: number) =>
    http.post<{ sent: boolean; expires_in: number }>('/financial/withdrawals/email-code', { establishment_id, amount_cents }),
  requestWithdrawal: (
    body: { establishment_id: string; amount_cents: number; totp_code: string; financial_password: string; email_code: string },
    idempotencyKey: string,
  ) => http.post<Withdrawal>('/financial/withdrawals', body, { idempotencyKey }),

  pixKeys: (establishmentId: string) => http.get<Paginated<PixKey>>(`/financial/establishments/${establishmentId}/pix-keys`),
  setPixKey: (
    establishmentId: string,
    body: { key_type: string; key_value: string; holder_name: string; holder_document: string; reason: string },
  ) => http.post<PixKey>(`/financial/establishments/${establishmentId}/pix-keys`, body),
  revokePixKey: (keyId: string, reason: string) => http.post<PixKey>(`/financial/pix-keys/${keyId}/revoke`, { reason }),

  establishmentBalance: (establishmentId: string) =>
    http.get<EstablishmentBalance>(`/financial/establishments/${establishmentId}/balance`),
  commissions: (q: Q) => http.get<Paginated<CommissionRow>>('/financial/commissions', { query: q }),
  commissionModels: () => http.get<CommissionModels>('/financial/commissions/models'),
  campaignCommissionTerms: (campaignId: string) => http.get<CampaignCommissionTerms>(`/financial/commissions/campaigns/${campaignId}`),
  commissionSettings: (establishmentId: string) =>
    http.get<CommissionSettings>(`/financial/commission-settings/establishments/${establishmentId}`),
  closeDueCompetences: () =>
    http.post<{ closed: Array<{ competence: string; commission_cents: number; residue_cents: number; released_cents: number }> }>(
      '/financial/commissions/close-due',
    ),

  refunds: (q: Q) => http.get<Paginated<Refund>>('/financial/refunds', { query: q }),
}

// ─── Apoio: estabelecimentos e organizações (W-ORG) ─────────────────────────

export interface EstablishmentLite {
  id: string
  org_id: string
  name: string
  trade_name?: string | null
  city?: string | null
  state?: string | null
  timezone?: string | null
}

export interface OrganizationLite {
  id: string
  name: string
  trade_name?: string | null
  business_roles?: string[]
}

export const orgApi = {
  establishments: (q: Q) => http.get<Paginated<EstablishmentLite>>('/establishments', { query: q }),
  organizations: (q: Q) => http.get<Paginated<OrganizationLite>>('/organizations', { query: q }),
}
