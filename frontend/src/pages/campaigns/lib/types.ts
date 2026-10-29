/**
 * Tipos do domínio de campanhas/mídia/preços/conflitos/inventário — espelham as respostas
 * reais do backend (W-CAMP, W-MEDIA, W-ORG, W-INV, W-FIN) lidas do código dos endpoints.
 * Dinheiro sempre em centavos (`*_cents`), percentuais em bps, datas ISO-8601 UTC.
 */

// ─── Mídia (W-MEDIA) ──────────────────────────────────────────────────────────
export type MediaStatus = 'UPLOADED' | 'VALIDATING' | 'PROCESSING' | 'READY' | 'REJECTED' | 'FAILED'
export type MediaKind = 'IMAGE' | 'VIDEO'
export type Orientation = 'HORIZONTAL' | 'VERTICAL'

export interface MediaSource {
  format: string
  mime: string
  size_bytes: number
  sha256: string
  width: number | null
  height: number | null
  duration_ms: number | null
  codec: string | null
  fps_milli: number | null
  has_audio: boolean | null
}

export interface MediaOutput {
  mime_type: string
  size_bytes: number
  sha256: string
  width: number
  height: number
  duration_ms: number | null
  codec: string
  fps_milli: number | null
  orientation: Orientation
  animated: boolean
  frame_count: number | null
  has_audio: boolean | null
}

export interface MediaFile {
  id: string
  org_id: string
  name: string
  original_filename: string
  origin: string
  kind: MediaKind
  status: MediaStatus
  status_code: string | null
  status_message: string | null
  progress_pct: number
  attempts: number
  extension_mismatch: boolean
  source: MediaSource
  output: MediaOutput | null
  has_preview: boolean
  content_url: string | null
  preview_url: string | null
  created_by: string | null
  created_at: string
  updated_at: string
  ready_at: string | null
  duplicate?: boolean
}

export interface MediaLimits {
  image: { formats: string[]; extensions: string[]; max_bytes: number }
  video: { formats: string[]; extensions: string[]; max_bytes: number; audio: string; output: string }
  resolutions: Array<{ width: number; height: number; orientation: Orientation; converted_to?: string }>
  not_allowed: string[]
}

export interface MediaUsage {
  media_id: string
  in_use: boolean
  references: Record<string, number>
}

/** Evento WS `media.processing`. */
export interface MediaProcessingEvent {
  id: string
  org_id?: string
  status?: MediaStatus
  progress?: number
  stage?: string
  code?: string | null
  message?: string | null
}

// ─── Campanhas (W-CAMP) ───────────────────────────────────────────────────────
export type CampaignStatus =
  | 'RASCUNHO'
  | 'AGUARDANDO_PAGAMENTO'
  | 'AGUARDANDO_MIDIA'
  | 'EM_ANALISE'
  | 'APROVADA'
  | 'AGENDADA'
  | 'SINCRONIZANDO'
  | 'ATIVA'
  | 'PAUSADA'
  | 'FINALIZADA'
  | 'REJEITADA'
  | 'CANCELADA'

export type PeriodType = 'AVULSO' | 'MENSAL' | 'SEMESTRAL' | 'ANUAL'
export type FreqKind = 'PER_HOUR' | 'PER_DAY' | 'SPECIFIC_TIME'
export type Priority = 'STANDARD' | 'PREMIUM'
export type Exclusivity = 'NONE' | 'COMPETITOR_EXCLUSIVE'
export type TargetType =
  | 'COUNTRY'
  | 'REGION'
  | 'STATE'
  | 'CITY'
  | 'NEIGHBORHOOD'
  | 'POSTAL_CODE'
  | 'CATEGORY'
  | 'ESTABLISHMENT'

export interface Target {
  type: TargetType
  value: string
  exclude: boolean
}

export interface Campaign {
  id: string
  org_id: string
  name: string
  description: string | null
  status: CampaignStatus
  version: number
  sales_mode: 'SELF_SERVICE' | 'COMMERCIAL'
  plan_id: string | null
  period_type: PeriodType
  start_date: string | null
  end_date: string | null
  duration_s: number
  freq_kind: FreqKind
  freq_value: number
  specific_times: string[]
  min_interval_s: number
  days_mask: number
  time_bands: number[][]
  priority: Priority
  exclusivity: Exclusivity
  allow_same_advertiser_consecutive: boolean
  request_art_service: boolean
  auto_renew?: boolean
  product_id: string | null
  brand_id: string | null
  contracted_price_cents: number
  paid_at: string | null
  approval_source: string | null
  submitted_at: string | null
  review_due_at: string | null
  approved_at: string | null
  rejected_at: string | null
  rejection_reason: string | null
  has_live_version: boolean
  has_pending_change: boolean
  playable: boolean
  payment_issue: string | null
  created_at: string
  updated_at: string
  /** Cobrança W-FIN da compra vigente (detalhe/lista). */
  paid_payment_order_id?: string | null
  /**
   * Estado derivado REPRODUZINDO (lista e detalhe, W-CAMP): `true` ⇔ ATIVA + exibição comprovada
   * (PoP válido) na janela do servidor; `null` = domínio de playback indisponível (nada é exibido).
   */
  is_reproducing?: boolean | null
  /** "REPRODUZINDO" quando ATIVA e reproduzindo; senão o status gravado. */
  display_status?: CampaignStatus | 'REPRODUZINDO'
  /** Só no detalhe: `{playing, last_valid_play_at, source, window_minutes}`. */
  reproducing?: PlayingState | null
}

/** Renovação (W-CAMP pós-janela): a proposta NÃO reserva inventário; a reserva de 2 h nasce no checkout. */
export interface RenewalProposal {
  campaign_id: string
  revision_id: string
  revision: number
  stage: 'PROPOSTA' | 'AGUARDANDO_PAGAMENTO' | 'APLICADA' | string
  revision_status: string
  auto: boolean
  periods: number | null
  previous_end_date: string | null
  new_end_date: string | null
  extension_start: string | null
  screens_count: number
  total_cents: number
  deadline: string | null
  quote: Quote | null
  order: CheckoutResponse | null
}

export interface CampaignCreativeLink {
  creative_id: string
  code: string | null
  name: string | null
  status: CreativeStatus | null
  technical_status: string | null
  is_active: boolean
  pending: boolean
  pending_removal: boolean
  replaces_creative_id: string | null
  order: number
}

export interface Revision {
  id: string
  number: number
  kind: string
  status: string
  spec: Record<string, unknown> | null
  screens_count: number
  creative_ids: string[]
  price_cents: number | null
  price_diff_cents: number | null
  change_summary: Record<string, unknown> | null
  quote: Quote | null
  created_at: string
  decided_at: string | null
  decision_reason: string | null
  screen_ids?: string[]
  hold_id?: string | null
}

export interface PlayingState {
  playing: boolean | null
  last_valid_play_at: string | null
  source: string
  window_minutes?: number
}

export interface CampaignDetail extends Campaign {
  targets: Target[]
  segment_ids: string[]
  creatives: CampaignCreativeLink[]
  allowed_transitions: Record<string, string[]>
  reproducing: PlayingState
  live_revision: Revision | null
  pending_revision: Revision | null
}

export interface ReviewQueueItem extends CampaignDetail {
  sla_overdue: boolean
  is_change_request: boolean
}

export interface HistoryItem {
  from: string | null
  to: string
  trigger: string
  reason: string | null
  origin: string | null
  at: string
  actor_user_id?: string | null
}

export type CreativeStatus = 'RASCUNHO' | 'EM_ANALISE' | 'APROVADO' | 'REJEITADO' | 'SUBSTITUIDO' | 'ARQUIVADO'

export interface Creative {
  id: string
  code: string
  org_id: string
  name: string
  kind: string
  media_file_id: string
  duration_seconds: number
  orientation: Orientation | null
  media_duration_ms: number | null
  ad_text: string | null
  status: CreativeStatus
  technical_status: string
  technical_error: string | null
  product_id: string | null
  brand_id: string | null
  segment_ids: string[]
  version: number
  supersedes_id: string | null
  root_id: string | null
  rejection_reason: string | null
  created_at: string
  updated_at: string
}

export interface AvailabilitySummary {
  screens_available: number
  establishments_count: number
  screens_letterbox: number
  excluded_by_duration: number
  excluded_by_conflict_establishments: number
  by_region: Array<{ region: string; screens: number }>
  by_state: Array<{ state: string; screens: number }>
  /** ``city``: nome de exibição (com acento) vindo do cadastro; ausente em respostas antigas. */
  by_city: Array<{ city_key: string; city?: string | null; screens: number }>
  /** SOMENTE para a equipe RD (AÇÃO 004): por que telas da seleção ficaram de fora — só contagens. */
  exclusions?: AvailabilityExclusions
}

export type OutOfInventoryReason = 'ESTABLISHMENT_INACTIVE' | 'AWAITING_CONTRACT' | 'SCREEN_INACTIVE' | 'NOT_SELLABLE'

export interface ExclusionCount {
  screens: number
  establishments: number
}

export interface AvailabilityExclusions {
  /** Telas que casam a segmentação e ficaram fora (inventário + duração + conflito). */
  screens_total: number
  out_of_inventory: ExclusionCount & {
    by_reason: Array<ExclusionCount & { reason: OutOfInventoryReason | string }>
    /** Das telas fora do inventário: quantas AINDA cairiam por conflito se entrassem (orientação). */
    also_conflict?: Array<ExclusionCount & { reason_code: string }>
  }
  duration: { screens: number }
  conflict: ExclusionCount & { by_reason: Array<ExclusionCount & { reason_code: string }> }
}

export interface ConflictStep {
  step?: number
  check?: string
  result?: string
  note?: string
  detail?: Record<string, unknown>
  creative_id?: string
  reason_code?: string
  rule_path?: ConflictStep[]
}

export interface ConflictDecision {
  result: 'ALLOW' | 'BLOCK' | string
  reason_code: string
  rule_path: ConflictStep[]
  engine_version: string
  facts_hash: string
  decision_id: string | null
}

export interface CampaignConflicts {
  allowed_screens: number
  blocked_establishments: number
  blocked?: Array<{ establishment_id: string } & ConflictDecision>
}

export interface ContractedScreen {
  friendly_id?: string | null
  state?: string | null
  orientation?: Orientation | null
  screen_id?: string
  establishment_id?: string
  city_key?: string | null
  letterbox?: boolean
  screen_class?: string
}

// ─── Preço (cotação do servidor) ──────────────────────────────────────────────
export interface QuoteLine {
  code: string
  kind: 'MEDIA' | 'DISCOUNT' | 'ADJUSTMENT' | 'SERVICE' | string
  amount_cents: number
  description: string
  ref?: string
}

export interface QuoteGroup {
  region: string | null
  state: string | null
  city_key: string | null
  screen_class: string
  venue_class: string
  screens: number
  unit_price_millicents: number
  media_cents: number
}

export interface MarginBreakdown {
  revenue_cents: number
  media_revenue_cents: number
  gateway_cents: number
  establishment_commission_cents: number
  seller_commission_cents: number
  taxes_cents: number
  server_cents: number
  equipment_cents: number
  designer_cents: number
  direct_costs_cents: number
  total_costs_cents: number
  margin_cents: number
  margin_bps: number
  min_margin_bps: number
  commission_pool_bps: number
  status: 'OK' | 'BELOW_MIN' | 'NEGATIVE'
}

export interface Quote {
  engine_version?: string
  currency?: string
  price_table?: { id?: string; version?: number; name?: string }
  inputs?: {
    duration_s: number
    freq_kind: FreqKind
    freq_value: number
    days_mask: number
    start_date: string
    end_date: string
    period_type: PeriodType
    screens_count: number
    contracted_days: number
    calendar_days: number
    coupon_code: string | null
    fees?: string[]
  }
  insertions?: { per_screen_per_day: string; total: number }
  lines: QuoteLine[]
  groups?: QuoteGroup[]
  media_gross_cents: number
  discount_cents: number
  min_price_adjustment_cents: number
  media_cents: number
  fees_cents: number
  total_cents: number
  margin?: MarginBreakdown
  quote_hash?: string
}

export interface QuoteResponse {
  quote: Quote
  availability: AvailabilitySummary
}

// ─── Checkout / pedidos ───────────────────────────────────────────────────────
export type OrderStatus = 'AGUARDANDO_PAGAMENTO' | 'PAGO' | 'EXPIRADO' | 'CANCELADO'

export interface PaymentPublic {
  id?: string
  status?: string
  amount_cents?: number
  expires_at?: string | null
  method?: string
  pix_copy_paste?: string | null
  pix_qr_code?: string | null
  checkout_url?: string | null
}

export interface OrderItem {
  campaign_id: string
  amount_cents: number
  media_cents: number
  fees_cents: number
  discount_cents: number
  quote_hash: string
  quote: Quote | null
}

export interface Order {
  id: string
  org_id: string
  kind: string
  status: OrderStatus
  media_cents: number
  fees_cents: number
  discount_cents: number
  credit_applied_cents: number
  total_cents: number
  payment_order_id: string | null
  expires_at: string | null
  paid_at: string | null
  created_at: string
  items?: OrderItem[]
  payment?: PaymentPublic | null
}

export interface CheckoutResponse {
  order_id: string
  status: OrderStatus
  kind: string
  total_cents: number
  expires_at: string
  payment: PaymentPublic
  warnings?: string[]
  error?: { code: string; message: string }
}

export interface RefundQuote {
  payment_order_id: string
  campaign_id?: string | null
  as_of?: string
  window_days?: number
  policy?: string | null
  allowed?: boolean
  media_refund_cents?: number
  services_refund_cents?: number
  refund_cents: number
  /** Presente quando o financeiro não conseguiu cotar esta cobrança. */
  error?: string
}

/** `GET /campaigns/{id}/cancel-quote` (W-CAMP → W-FIN `quote_refund` por cobrança paga). */
export interface CancelQuote {
  campaign_id: string
  status: string
  can_cancel: boolean
  paid: boolean
  payments: RefundQuote[]
  refund_total_cents: number
  note?: string
}

// ─── Taxonomia (W-ORG) ────────────────────────────────────────────────────────
export interface TreeItem {
  id: string
  parent_id: string | null
  name: string
  slug: string
  depth: number
  is_active: boolean
  ancestor_ids: string[]
  kind?: 'SECTOR' | 'CATEGORY' | 'SUBCATEGORY'
  policy?: 'BLOCK' | 'ALLOW' | null
}

export interface ContentPolicy {
  segment_id: string
  segment_name: string
  segment_slug: string
  effect: 'BLOCK' | 'ALLOW'
  reason: string
  is_default: boolean
  updated_at: string | null
}

export interface Brand {
  id: string
  name: string
  slug: string
  is_active: boolean
  owner_org_id: string | null
}

export interface Product {
  id: string
  name: string
  slug: string
  segment_id: string
  brand_id: string | null
  is_active: boolean
}

export interface CompetitorLink {
  id: string
  org_a_id: string
  org_b_id: string
  reason: string
  created_at: string | null
}

export type RuleSubjectType = 'EST_CATEGORY' | 'EST_SEGMENT' | 'EST_ORG' | 'ESTABLISHMENT'
export type RuleObjectType = 'AD_CATEGORY' | 'AD_SEGMENT' | 'AD_PRODUCT' | 'AD_BRAND' | 'AD_ADVERTISER'

export interface ConflictRule {
  id: string
  subject_type: RuleSubjectType
  subject_id: string
  object_type: RuleObjectType
  object_id: string
  include_descendants: boolean
  effect: 'BLOCK' | 'ALLOW'
  reason: string
  active: boolean
  valid_from: string | null
  valid_until: string | null
  version: number
  created_at: string
  updated_at: string | null
}

export interface ConflictException {
  id: string
  establishment_id: string
  object_type: 'ADVERTISER' | 'BRAND' | 'CAMPAIGN'
  object_id: string
  reason: string
  approved_by: string
  valid_from: string | null
  valid_until: string | null
  revoked_at: string | null
}

export interface ConflictOverride {
  id: string
  campaign_id: string
  establishment_id: string
  justification: string
  approved_by: string
  approved_at: string
  expires_at: string | null
  revoked_at: string | null
  revoke_reason: string | null
  decision_before?: ConflictDecision
  decision_after?: ConflictDecision
  warning?: string
}

// ─── Preços (W-CAMP pricing) ──────────────────────────────────────────────────
export interface PriceItem {
  id: string
  kind: string
  key: string
  mode: string
  min_value: number | null
  max_value: number | null
  value: number
  description: string | null
}

export interface PriceTable {
  id: string
  name: string
  version: number
  status: 'RASCUNHO' | 'ATIVA' | 'ARQUIVADA'
  notes: string | null
  cloned_from_id: string | null
  created_at: string
  activated_at: string | null
  archived_at: string | null
  items?: PriceItem[]
  warnings?: Array<{ code: string; plan_id?: string; name?: string; margin_bps?: number }>
}

export type PlanStatus = 'RASCUNHO' | 'PENDENTE_APROVACAO' | 'ATIVO' | 'INATIVO' | 'REJEITADO'

export interface Plan {
  id: string
  name: string
  slug: string
  description: string | null
  kind: 'PREDEFINED' | 'CUSTOM'
  org_id: string | null
  status: PlanStatus
  period_type: PeriodType
  period_days: number | null
  duration_s: number
  freq_kind: FreqKind
  freq_value: number
  specific_times: string[]
  min_interval_s: number
  days_mask: number
  time_bands: number[][]
  screens_min: number
  screens_max: number
  priority: Priority
  exclusivity: Exclusivity
  discount_bps: number
  max_campaigns: number | null
  max_creatives: number | null
  max_screens: number | null
  max_upload_mb: number | null
  version: number
  margin_bps?: number | null
  margin_status?: string | null
  approved_at?: string | null
  approved_by?: string | null
  created_by?: string | null
  rejection_reason?: string | null
  reference_total_cents?: number | null
  warnings?: Array<{ code: string; margin_bps?: number; min_margin_bps?: number }>
}

export interface Promotion {
  id: string
  name: string
  discount_bps: number
  valid_from: string
  valid_until: string
  plan_ids: string[] | null
  active: boolean
}

export interface Coupon {
  id: string
  code: string
  description: string | null
  discount_type: 'PERCENT' | 'FIXED'
  discount_bps: number
  discount_cents: number
  valid_from: string
  valid_until: string
  max_uses: number | null
  uses_count: number
  max_uses_per_org: number | null
  min_order_cents: number
  plan_ids: string[] | null
  active: boolean
}

export interface PriceFloor {
  key: string
  label: string
  unit: string
  min_price_cents: number
  note: string | null
  version: number
  updated_at: string | null
}

// ─── Inventário / scheduler (W-INV) ───────────────────────────────────────────
export interface RotationPolicy {
  id: string
  scope: 'GLOBAL' | 'PARTNER_PLAN' | 'ESTABLISHMENT' | 'SCREEN'
  scope_id: string | null
  version: number
  effective_from: string
  effective_to: string | null
  reason: string
  created_at: string
  internal_seconds: number
  external_seconds: number
  cycle_seconds: number
  cycles_per_hour: number
  units_per_block: number
  units_per_hour: number
  eligible_120s: boolean
  allow_external_overflow: boolean
  overflow_max_seconds: number
}

export interface InventoryAvailability {
  screens: number
  sellable_screens: number
  unit_seconds: number
  days: Array<{ date: string; capacity_units: number; used_units: number; free_units: number; occupancy_bps: number }>
}

export interface InventoryHold {
  id: string
  org_id: string
  campaign_id: string
  order_id: string | null
  status: 'HELD' | 'CONVERTED' | 'EXPIRED' | 'CANCELLED' | 'PAID_WITHOUT_INVENTORY' | string
  mode: string
  created_at: string
  hold_expires_at: string | null
  converted_at: string | null
  released_at: string | null
  release_reason: string | null
  screens_requested: number
  screens_held: number
  replaces_hold_id: string | null
}

export interface ScreenRetirement {
  id: string
  screen_id: string
  establishment_id: string | null
  reason: string | null
  affected: unknown
  status: 'OPEN' | 'RESOLVED'
  resolution: string | null
  resolved_by: string | null
  resolved_at: string | null
  created_at: string
}

export interface GridEntry {
  /** unidade de 30 s dentro do bloco externo */
  u: number
  /** duração em MILISSEGUNDOS (manifesto RDPP/1: `(u, identidade, d_ms)`) */
  d: number
  code: string
  kind: string
  campaign_id: string | null
  creative_id: string | null
}

export interface ScreenGrid {
  screen_id: string
  timezone: string
  rotation: { internal_ms?: number; external_ms?: number; cycle_ms: number; [k: string]: unknown }
  anchor_ms: number | null
  horizon: unknown
  internal_pattern: unknown
  fallback: unknown
  emergency: unknown
  revoked: unknown
  blocks: Array<{ bucket_ms: number; entries: GridEntry[] }>
  unplaced: unknown[]
  plan_sha256: string
}

export interface ManifestVersionInfo {
  screen_id: string
  version: number
  manifest_id: string
  generated_at: string
  reason: string | null
  content_sha256: string
  plan_sha256: string
  size_bytes: number
  key_id: string
  horizon_from: string | null
  horizon_to: string | null
  unplaced_count: number
  applied_at: string | null
  applied_by_device: string | null
  content_available: boolean
}

// ─── Entidades auxiliares (somente RD) ────────────────────────────────────────
export interface OrganizationLite {
  id: string
  name: string
  trade_name?: string | null
  document_formatted?: string | null
  business_roles?: string[]
}

export interface EstablishmentLite {
  id: string
  name: string
  city?: string | null
  state?: string | null
  city_key?: string | null
  timezone?: string | null
  org_id?: string
  is_active?: boolean
  /** ``AWAITING_CONTRACT`` ⇒ telas fora do inventário até o aceite do contrato de parceria. */
  onboarding_status?: string | null
}

export interface ScreenLite {
  id: string
  friendly_id: string
  name: string | null
  status: string
  orientation: Orientation | null
  establishment?: { id: string; name: string; city: string | null; state: string | null; timezone: string | null }
}
