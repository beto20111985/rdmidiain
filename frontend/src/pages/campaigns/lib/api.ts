/**
 * Chamadas da API do domínio (contratos lidos do código dos endpoints do backend).
 * Toda chamada passa pelo cliente HTTP da fundação (Bearer em memória, X-RD-Context,
 * envelope de erro). Operações sensíveis recebem `Idempotency-Key`.
 */
import {
  buildUrl,
  getActiveContextHeader,
  http,
  newRequestId,
  refreshAccessToken,
  request,
  tokenStore,
  type Paginated,
  type QueryParams,
} from '@/lib/http'
import { toApiError } from '@/lib/errors'
import type {
  AvailabilitySummary,
  Brand,
  Campaign,
  CampaignConflicts,
  CampaignDetail,
  CancelQuote,
  CheckoutResponse,
  CompetitorLink,
  ConflictDecision,
  ConflictException,
  ConflictOverride,
  ConflictRule,
  ContentPolicy,
  ContractedScreen,
  Coupon,
  Creative,
  EstablishmentLite,
  HistoryItem,
  InventoryAvailability,
  InventoryHold,
  ManifestVersionInfo,
  MarginBreakdown,
  MediaFile,
  MediaLimits,
  MediaUsage,
  Order,
  OrganizationLite,
  Plan,
  PriceFloor,
  PriceTable,
  Promotion,
  Product,
  Quote,
  QuoteResponse,
  ReviewQueueItem,
  RenewalProposal,
  Revision,
  RotationPolicy,
  ScreenGrid,
  ScreenLite,
  ScreenRetirement,
  Target,
  TreeItem,
} from './types'

type Items<T> = { items: T[] }

// ─── Mídia ───────────────────────────────────────────────────────────────────
export const mediaApi = {
  limits: () => http.get<MediaLimits>('/media/limits'),
  list: (query: QueryParams) => http.get<Paginated<MediaFile>>('/media', { query }),
  get: (id: string) => http.get<MediaFile>(`/media/${id}`),
  usage: (id: string) => http.get<MediaUsage>(`/media/${id}/usage`),
  rename: (id: string, name: string) => http.patch<MediaFile>(`/media/${id}`, { name }),
  retry: (id: string) => http.post<MediaFile>(`/media/${id}/retry`),
  remove: (id: string, reason?: string) => http.delete<void>(`/media/${id}`, { query: { reason } }),
}

/**
 * Busca um arquivo protegido (conteúdo/preview) com Bearer e devolve um Blob — não existe
 * URL com token (W-MEDIA removeu `?token=`). Quem chama cria/revoga o object URL.
 */
export async function fetchProtectedBlob(path: string, signal?: AbortSignal): Promise<Blob> {
  const url = path.startsWith('/api/') ? path : buildUrl(path)
  const doFetch = () => {
    const headers: Record<string, string> = { 'X-Request-ID': newRequestId() }
    const t = tokenStore.get()
    if (t) headers.Authorization = `Bearer ${t}`
    const ctx = getActiveContextHeader()
    if (ctx) headers['X-RD-Context'] = ctx
    return fetch(url, { headers, credentials: 'omit', signal })
  }
  let res = await doFetch()
  if (res.status === 401 && (await refreshAccessToken())) res = await doFetch()
  if (!res.ok) {
    let body: unknown
    try {
      body = await res.json()
    } catch {
      body = undefined
    }
    throw toApiError(res.status, body, { requestId: res.headers.get('x-request-id') ?? undefined })
  }
  return res.blob()
}

// ─── Campanhas ───────────────────────────────────────────────────────────────
export interface CampaignWrite {
  name?: string
  description?: string | null
  org_id?: string
  period_type?: string
  period_days?: number
  start_date?: string | null
  end_date?: string | null
  duration_s?: number
  freq_kind?: string
  freq_value?: number
  specific_times?: string[]
  min_interval_s?: number
  days_mask?: number
  time_bands?: number[][]
  priority?: string
  exclusivity?: string
  allow_same_advertiser_consecutive?: boolean
  request_art_service?: boolean
  auto_renew?: boolean
  plan_id?: string | null
  product_id?: string | null
  brand_id?: string | null
  targets?: Target[]
  segment_ids?: string[]
  expected_version?: number
}

export const campaignsApi = {
  list: (query: QueryParams) => http.get<Paginated<Campaign>>('/campaigns', { query }),
  get: (id: string) => http.get<CampaignDetail>(`/campaigns/${id}`),
  create: (body: CampaignWrite) => http.post<CampaignDetail>('/campaigns', body),
  update: (id: string, body: CampaignWrite) => http.patch<CampaignDetail>(`/campaigns/${id}`, body),
  setTargets: (id: string, targets: Target[]) => http.put<{ targets: Target[] }>(`/campaigns/${id}/targets`, { targets }),
  setSegments: (id: string, segment_ids: string[]) => http.put<{ segment_ids: string[] }>(`/campaigns/${id}/segments`, { segment_ids }),
  linkCreative: (id: string, creative_id: string, order = 0) =>
    http.post<{ creative_id: string }>(`/campaigns/${id}/creatives`, { creative_id, order }),
  unlinkCreative: (id: string, creative_id: string) => http.delete<void>(`/campaigns/${id}/creatives/${creative_id}`),
  quote: (id: string, coupon_code?: string | null) =>
    http.post<QuoteResponse>(`/campaigns/${id}/quote`, coupon_code ? { coupon_code } : {}),
  /** Com `campaign_id`: mesma elegibilidade do checkout/aprovação (conflitos dos anúncios aplicados). */
  availability: (body: { campaign_id?: string; org_id?: string; targets: Target[]; duration_s: number }) =>
    http.post<AvailabilitySummary>('/campaigns/availability', body),
  conflicts: (id: string) => http.get<CampaignConflicts>(`/campaigns/${id}/conflicts`),
  screens: (id: string) =>
    http.get<{ source?: 'CONTRATADAS' | 'PREVISAO'; items: ContractedScreen[]; total?: number; summary?: Partial<AvailabilitySummary> }>(`/campaigns/${id}/screens`),
  history: (id: string) => http.get<Items<HistoryItem>>(`/campaigns/${id}/history`),
  revisions: (id: string) => http.get<Items<Revision>>(`/campaigns/${id}/revisions`),
  pause: (id: string, reason?: string) => http.post<CampaignDetail>(`/campaigns/${id}/pause`, { reason: reason || null }),
  resume: (id: string) => http.post<CampaignDetail>(`/campaigns/${id}/resume`),
  cancel: (id: string, reason: string) => http.post<CampaignDetail>(`/campaigns/${id}/cancel`, { reason }),
  cancelQuote: (id: string) => http.get<CancelQuote>(`/campaigns/${id}/cancel-quote`),
  duplicate: (id: string) => http.post<CampaignDetail>(`/campaigns/${id}/duplicate`),
  submitMedia: (id: string) => http.post<CampaignDetail>(`/campaigns/${id}/submit-media`),
  resubmit: (id: string) => http.post<CampaignDetail>(`/campaigns/${id}/resubmit`),
  /** Proposta de renovação (cotação do período adicional) — NÃO reserva nem cobra. 201 nova | 200 a mesma. */
  renew: (id: string, periods: number) => http.post<RenewalProposal>(`/campaigns/${id}/renew`, { periods }),
  /** Renovação aberta da campanha (404 RENEWAL_NOT_FOUND quando não há). */
  renewal: (id: string) => http.get<RenewalProposal>(`/campaigns/${id}/renewal`),
  /** Checkout da renovação: reserva de 2 h + pedido + cobrança PIX (Idempotency-Key por tentativa). */
  renewCheckout: (id: string, idempotencyKey: string) =>
    http.post<CheckoutResponse>(`/campaigns/${id}/renew/checkout`, { method: 'pix' }, { idempotencyKey }),
  setAutoRenew: (id: string, auto_renew: boolean) => http.put<Campaign>(`/campaigns/${id}/auto-renew`, { auto_renew }),
  requestChange: (id: string, body: Record<string, unknown>, idempotencyKey: string) =>
    http.post<ChangeResponse>(`/campaigns/${id}/changes`, body, { idempotencyKey }),
  byCode: (code: string) =>
    http.get<{ creative: Creative; campaigns: Array<{ id: string; name: string; status: string }> }>(
      `/campaigns/by-code/${encodeURIComponent(code)}`,
    ),
  // RD
  reviewQueue: (query: QueryParams) => http.get<Paginated<ReviewQueueItem>>('/campaigns/review-queue', { query }),
  approve: (id: string, note: string | null, idempotencyKey: string) =>
    http.post<CampaignDetail & { conflicts_blocked_establishments?: unknown }>(`/campaigns/${id}/approve`, { note }, { idempotencyKey }),
  reject: (id: string, reason: string, creative_ids: string[] | null, idempotencyKey: string) =>
    http.post<CampaignDetail>(`/campaigns/${id}/reject`, { reason, creative_ids }, { idempotencyKey }),
  bulk: (campaign_ids: string[], action: 'approve' | 'reject', reason: string | null, idempotencyKey: string) =>
    http.post<{ action: string; done: string[]; failed: Array<{ campaign_id: string; code: string; message: string }> }>(
      '/campaigns/approvals/bulk',
      { campaign_ids, action, reason },
      { idempotencyKey },
    ),
  kill: (id: string, reason: string, idempotencyKey: string) =>
    http.post<CampaignDetail>(`/campaigns/${id}/kill`, { reason }, { idempotencyKey }),
  approvalPolicies: () =>
    http.get<{ global_mode: string; items: Array<{ org_id: string; mode: string; reason: string; updated_at: string }> }>(
      '/campaigns/approval-policies',
    ),
  setApprovalPolicy: (orgId: string, mode: 'MANUAL' | 'AUTO', reason: string) =>
    http.put<unknown>(`/campaigns/approval-policies/${orgId}`, { mode, reason }),
  removeApprovalPolicy: (orgId: string) => http.delete<void>(`/campaigns/approval-policies/${orgId}`),
}

export interface ChangeResponse {
  campaign_id: string
  revision: number
  revision_id: string
  status: string
  changed_fields: string[]
  previous_price_cents: number
  new_price_cents: number
  diff_cents: number
  credit_due_cents: number
  quote: Quote | null
  order: { order_id: string; status: string; total_cents: number; expires_at: string } | null
  campaign_status: string
}

// ─── Criativos ───────────────────────────────────────────────────────────────
export const creativesApi = {
  list: (query: QueryParams) => http.get<Paginated<Creative>>('/creatives', { query }),
  get: (id: string) => http.get<Creative>(`/creatives/${id}`),
  /** Revisão RD de criativo INTERNAL/INSTITUTIONAL/FALLBACK (EXTERNAL é revisado pela campanha). */
  review: (id: string, decision: 'APPROVE' | 'REJECT', reason?: string | null) =>
    http.post<Creative>(`/creatives/${id}/review`, reason ? { decision, reason } : { decision }),
  create: (body: {
    org_id?: string
    name: string
    media_file_id: string
    duration_seconds: number
    ad_text?: string | null
    product_id?: string | null
    brand_id?: string | null
    segment_ids?: string[]
  }) => http.post<Creative>('/creatives', body),
}

// ─── Checkout ────────────────────────────────────────────────────────────────
export const checkoutApi = {
  create: (campaign_ids: string[], coupon_code: string | null, idempotencyKey: string) =>
    request<CheckoutResponse>('/checkout', {
      method: 'POST',
      body: coupon_code ? { campaign_ids, coupon_code } : { campaign_ids },
      idempotencyKey,
    }),
  orders: (query: QueryParams) => http.get<Paginated<Order>>('/checkout/orders', { query }),
  order: (id: string) => http.get<Order>(`/checkout/orders/${id}`),
  cancelOrder: (id: string) => http.post<Order>(`/checkout/orders/${id}/cancel`),
  validateCoupon: (code: string) =>
    http.post<{ valid: boolean; code: string; discount_type: string; discount_bps: number; discount_cents: number; min_order_cents: number; valid_until: string }>(
      '/pricing/coupons/validate',
      { code },
    ),
}

// ─── Preços ──────────────────────────────────────────────────────────────────
export const pricingApi = {
  tables: () => http.get<Items<PriceTable>>('/pricing/tables'),
  table: (id: string) => http.get<PriceTable>(`/pricing/tables/${id}`),
  createTable: (name: string, notes: string | null) => http.post<PriceTable>('/pricing/tables', { name, notes }),
  duplicateTable: (id: string) => http.post<PriceTable>(`/pricing/tables/${id}/duplicate`),
  activateTable: (id: string) => http.post<PriceTable>(`/pricing/tables/${id}/activate`),
  addItem: (id: string, body: Record<string, unknown>) => http.post<PriceTable>(`/pricing/tables/${id}/items`, body),
  patchItem: (id: string, itemId: string, body: Record<string, unknown>) =>
    http.patch<PriceTable>(`/pricing/tables/${id}/items/${itemId}`, body),
  deleteItem: (id: string, itemId: string) => http.delete<void>(`/pricing/tables/${id}/items/${itemId}`),
  plans: (query: QueryParams) => http.get<Paginated<Plan>>('/pricing/plans', { query }),
  catalog: (orgId?: string | null) => http.get<Items<Plan>>('/pricing/plans/catalog', { query: { org_id: orgId ?? undefined } }),
  createPlan: (body: Record<string, unknown>) => http.post<Plan>('/pricing/plans', body),
  patchPlan: (id: string, body: Record<string, unknown>) => http.patch<Plan>(`/pricing/plans/${id}`, body),
  duplicatePlan: (id: string) => http.post<Plan>(`/pricing/plans/${id}/duplicate`),
  activatePlan: (id: string) => http.post<Plan>(`/pricing/plans/${id}/activate`),
  deactivatePlan: (id: string) => http.post<Plan>(`/pricing/plans/${id}/deactivate`),
  approvePlan: (id: string) => http.post<Plan>(`/pricing/plans/${id}/approve`),
  rejectPlan: (id: string, reason: string) => http.post<Plan>(`/pricing/plans/${id}/reject`, { reason }),
  planMargin: (id: string) => http.post<{ plan_id: string; quote: Quote }>(`/pricing/plans/${id}/margin`),
  promotions: () => http.get<Items<Promotion>>('/pricing/promotions'),
  createPromotion: (body: Record<string, unknown>) => http.post<Promotion>('/pricing/promotions', body),
  patchPromotion: (id: string, body: Record<string, unknown>) => http.patch<Promotion>(`/pricing/promotions/${id}`, body),
  coupons: () => http.get<Items<Coupon>>('/pricing/coupons'),
  createCoupon: (body: Record<string, unknown>) => http.post<Coupon>('/pricing/coupons', body),
  patchCoupon: (id: string, body: Record<string, unknown>) => http.patch<Coupon>(`/pricing/coupons/${id}`, body),
  quotePreview: (body: Record<string, unknown>) => http.post<QuoteResponse>('/pricing/quote', body),
  marginSimulator: (body: Record<string, unknown>) =>
    http.post<{ quote: Quote; margin: MarginBreakdown }>('/pricing/margin-simulator', body),
  floors: () => http.get<Items<PriceFloor>>('/instance/price-floors'),
  checkFloor: (floor_key: string, price_cents: number) =>
    http.post<{ ok: boolean; floor_key: string; min_price_cents: number | null }>('/instance/price-floors/check', {
      floor_key,
      price_cents,
    }),
}

// ─── Taxonomia e conflitos ───────────────────────────────────────────────────
export const taxonomyApi = {
  nodes: (includeInactive = false) =>
    http.get<Paginated<TreeItem>>('/taxonomy/nodes', { query: { include_inactive: includeInactive || undefined } }),
  createNode: (body: { kind: string; name: string; parent_id?: string | null }) => http.post<TreeItem>('/taxonomy/nodes', body),
  patchNode: (id: string, body: Record<string, unknown>) => http.patch<TreeItem>(`/taxonomy/nodes/${id}`, body),
  segments: (includeInactive = false) =>
    http.get<Paginated<TreeItem>>('/taxonomy/segments', { query: { include_inactive: includeInactive || undefined } }),
  createSegment: (body: { name: string; parent_id?: string | null }) => http.post<TreeItem>('/taxonomy/segments', body),
  patchSegment: (id: string, body: Record<string, unknown>) => http.patch<TreeItem>(`/taxonomy/segments/${id}`, body),
  segmentPolicy: (id: string) =>
    http.get<{ effect: string; source_segment_id: string | null; reason: string }>(`/taxonomy/segments/${id}/policy`),
  policies: () => http.get<Paginated<ContentPolicy>>('/taxonomy/content-policies'),
  setPolicy: (segmentId: string, effect: 'BLOCK' | 'ALLOW', reason: string) =>
    http.put<unknown>(`/taxonomy/content-policies/${segmentId}`, { effect, reason }),
  removePolicy: (segmentId: string, reason: string) => http.post<unknown>(`/taxonomy/content-policies/${segmentId}/remove`, { reason }),
  brands: (query: QueryParams) => http.get<Paginated<Brand>>('/taxonomy/brands', { query }),
  createBrand: (body: { name: string; owner_org_id?: string | null }) => http.post<Brand>('/taxonomy/brands', body),
  patchBrand: (id: string, body: Record<string, unknown>) => http.patch<Brand>(`/taxonomy/brands/${id}`, body),
  products: (query: QueryParams) => http.get<Paginated<Product>>('/taxonomy/products', { query }),
  createProduct: (body: { name: string; segment_id: string; brand_id?: string | null }) => http.post<Product>('/taxonomy/products', body),
  patchProduct: (id: string, body: Record<string, unknown>) => http.patch<Product>(`/taxonomy/products/${id}`, body),
  competitors: () => http.get<Paginated<CompetitorLink>>('/taxonomy/competitor-links'),
  createCompetitor: (body: { org_a_id: string; org_b_id: string; reason: string }) =>
    http.post<CompetitorLink>('/taxonomy/competitor-links', body),
  removeCompetitor: (id: string, reason: string) => http.post<unknown>(`/taxonomy/competitor-links/${id}/remove`, { reason }),
}

export const conflictsApi = {
  rules: (query: QueryParams) => http.get<Paginated<ConflictRule>>('/conflicts/rules', { query }),
  createRule: (body: Record<string, unknown>) => http.post<ConflictRule>('/conflicts/rules', body),
  patchRule: (id: string, body: Record<string, unknown>) => http.patch<ConflictRule>(`/conflicts/rules/${id}`, body),
  exceptions: (establishment_id?: string) =>
    http.get<Items<ConflictException>>('/conflicts/exceptions', { query: { establishment_id } }),
  createException: (body: Record<string, unknown>) => http.post<ConflictException>('/conflicts/exceptions', body),
  revokeException: (id: string, reason: string) => http.post<ConflictException>(`/conflicts/exceptions/${id}/revoke`, { reason }),
  overrides: (campaign_id?: string) => http.get<Items<ConflictOverride>>('/conflicts/overrides', { query: { campaign_id } }),
  createOverride: (body: Record<string, unknown>) => http.post<ConflictOverride>('/conflicts/overrides', body),
  revokeOverride: (id: string, reason: string) => http.post<ConflictOverride>(`/conflicts/overrides/${id}/revoke`, { reason }),
  evaluate: (body: Record<string, unknown>) =>
    http.post<ConflictDecision & { mode: string; competitors?: boolean }>('/conflicts/evaluate', body),
}

// ─── Inventário / scheduler ──────────────────────────────────────────────────
export const inventoryApi = {
  policies: (query: QueryParams) =>
    http.get<Paginated<RotationPolicy> & { default: Partial<RotationPolicy> | null }>('/inventory/policies', { query }),
  createPolicy: (body: Record<string, unknown>) => http.post<RotationPolicy>('/inventory/policies', body),
  availability: (query: QueryParams) => http.get<InventoryAvailability>('/inventory/availability', { query }),
  holds: (query: QueryParams) => http.get<Paginated<InventoryHold>>('/inventory/holds', { query }),
  retirements: (query: QueryParams) => http.get<Paginated<ScreenRetirement>>('/inventory/retirements', { query }),
  resolveRetirement: (id: string, resolution: string) =>
    http.post<ScreenRetirement>(`/inventory/retirements/${id}/resolve`, { resolution }),
  screenState: (id: string) => http.get<Record<string, unknown>>(`/inventory/screens/${id}/state`),
  grid: (id: string, hours: number) => http.get<ScreenGrid>(`/scheduler/screens/${id}/grid`, { query: { hours } }),
  manifests: (id: string, query: QueryParams) =>
    http.get<Paginated<ManifestVersionInfo>>(`/scheduler/screens/${id}/manifests`, { query }),
}

// ─── Entidades auxiliares (somente equipe RD — nunca chamadas no contexto anunciante) ─
export const lookupApi = {
  organizations: (query: QueryParams) => http.get<Paginated<OrganizationLite>>('/organizations', { query }),
  organization: (id: string) => http.get<OrganizationLite>(`/organizations/${id}`),
  establishments: (query: QueryParams) => http.get<Paginated<EstablishmentLite>>('/establishments', { query }),
  establishment: (id: string) => http.get<EstablishmentLite>(`/establishments/${id}`),
  screens: (query: QueryParams) => http.get<Paginated<ScreenLite>>('/screens', { query }),
}
