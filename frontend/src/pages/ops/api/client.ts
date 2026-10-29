/**
 * Chamadas HTTP do domínio de Operação (FE-OPS). Sempre via `http` da fundação
 * (Bearer em memória, X-Request-ID, X-RD-Context, envelope de erro → ApiError).
 * Contratos conferidos com o OpenAPI do servidor vivo (pós-janela, migração 0005).
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
import { uploadWithProgress } from '@/lib/upload'
import type {
  AnchorHistoryEntry,
  AudienceEstimate,
  AuditRow,
  ComodatoAgreement,
  ContractAcceptance,
  ContractTemplate,
  CreativeLite,
  Device,
  DeviceCommand,
  Emergency,
  EquipmentItem,
  EquipmentMovement,
  EquipmentSummary,
  Establishment,
  FallbackItem,
  Installation,
  InstallationPhoto,
  InternalItem,
  MaintenanceOrder,
  ManifestVersionMeta,
  MediaLite,
  OnboardingApplication,
  OnboardingDocument,
  Organization,
  PartnerPlan,
  PresenceSummary,
  Product,
  RdPieceKind,
  Screen,
  ScreenGrid,
  ScreenHistoryEntry,
  ScreenInventoryState,
  ScreenPresence,
  ScreenProgramming,
  TaxonomyNode,
  TimezoneSuggestion,
  UserLite,
} from './types'

type Body = Record<string, unknown>
const list = <T,>(path: string, query?: QueryParams) => http.get<Paginated<T>>(path, { query })

// --------------------------------------------------------------------------- Estabelecimentos
export const establishmentsApi = {
  list: (q: QueryParams) => list<Establishment>('/establishments', q),
  get: (id: string) => http.get<Establishment>(`/establishments/${id}`),
  create: (body: Body) => http.post<Establishment>('/establishments', body),
  update: (id: string, body: Body) => http.patch<Establishment>(`/establishments/${id}`, body),
  setGeo: (id: string, body: { lat: number; lng: number; source: 'MANUAL' | 'GPS'; accuracy_m?: number | null }) =>
    http.put<Establishment>(`/establishments/${id}/geo`, body),
  setSegments: (id: string, ids: string[]) => http.put<{ segment_ids: string[] }>(`/establishments/${id}/segments`, { ids }),
  setProducts: (id: string, ids: string[]) => http.put<{ product_ids: string[] }>(`/establishments/${id}/products`, { ids }),
  deactivate: (id: string, reason: string) => http.post<Establishment>(`/establishments/${id}/deactivate`, { reason }),
  reactivate: (id: string, reason: string) => http.post<Establishment>(`/establishments/${id}/reactivate`, { reason }),
  timezoneSuggestion: (state: string, city?: string) =>
    http.get<TimezoneSuggestion>('/establishments/timezone-suggestion', { query: { state, city } }),
  audience: (id: string) => list<AudienceEstimate>(`/establishments/${id}/audience-estimates`, { page_size: 10 }),
  addAudience: (id: string, body: Body) => http.post<AudienceEstimate>(`/establishments/${id}/audience-estimates`, body),
  setTechPin: (id: string, body: { pin: string; reason: string }) =>
    http.post<Paginated<DeviceCommand>>(`/establishments/${id}/tech-pin`, body, { idempotencyKey: newRequestId() }),
}

export const taxonomyApi = {
  nodes: () => http.get<Paginated<TaxonomyNode>>('/taxonomy/nodes'),
  segments: () => http.get<Paginated<TaxonomyNode>>('/taxonomy/segments'),
  products: (q: QueryParams) => list<Product>('/taxonomy/products', q),
}

export const organizationsApi = {
  list: (q: QueryParams) => list<Organization>('/organizations', q),
  get: (id: string) => http.get<Organization>(`/organizations/${id}`),
}

// --------------------------------------------------------------------------- Telas / dispositivos
export const screensApi = {
  list: (q: QueryParams) => list<Screen>('/screens', q),
  get: (id: string) => http.get<Screen>(`/screens/${id}`),
  create: (body: Body) => http.post<Screen>('/screens', body),
  update: (id: string, body: Body) => http.patch<Screen>(`/screens/${id}`, body),
  setStatus: (id: string, body: { status: string; reason: string }) => http.post<Screen>(`/screens/${id}/status`, body),
  move: (id: string, body: { establishment_id: string; reason: string }) => http.post<Screen>(`/screens/${id}/move`, body),
  replaceHardware: (id: string, body: Body) => http.post<{ screen: Screen }>(`/screens/${id}/replace-hardware`, body),
  decommission: (id: string, reason: string) => http.post<Screen>(`/screens/${id}/decommission`, { reason }),
  history: (id: string, page = 1) => list<ScreenHistoryEntry>(`/screens/${id}/history`, { page, page_size: 25 }),
  presence: (id: string) => http.get<ScreenPresence>(`/screens/${id}/presence`),
  commands: (id: string, page = 1) => list<DeviceCommand>(`/screens/${id}/commands`, { page, page_size: 10 }),
  sendCommand: (id: string, body: { command: string; reason: string; confirm: boolean; args?: Body }) =>
    http.post<DeviceCommand>(`/screens/${id}/commands`, body, { idempotencyKey: newRequestId() }),
  setTechPin: (id: string, body: { pin: string; reason: string }) => http.post<DeviceCommand>(`/screens/${id}/tech-pin`, body),
  pair: (id: string, pairing_code: string) =>
    http.post<{ device: Device; revoked_device_ids: string[]; pairing_request_id: string }>(`/screens/${id}/pairing`, {
      pairing_code,
    }),
  audience: (id: string) => list<AudienceEstimate>(`/screens/${id}/audience-estimates`, { page_size: 10 }),
  addAudience: (id: string, body: Body) => http.post<AudienceEstimate>(`/screens/${id}/audience-estimates`, body),
  revokeDevice: (deviceId: string, reason: string) => http.post<Device>(`/devices/${deviceId}/revoke`, { reason }),
  device: (deviceId: string) => http.get<Device>(`/devices/${deviceId}`),
}

/** Item de `GET /screens/presence` (W-PLAY): telas fora do escopo são omitidas. */
export interface PresenceBatchItem extends PresenceSummary {
  screen_id: string
}

/**
 * Presença de até 100 telas numa chamada (`GET /screens/presence?screen_id=…&screen_id=…`).
 * Telas ausentes da resposta (fora do escopo) ficam sem presença ("—").
 */
export async function screensPresenceBatch(ids: string[]): Promise<Record<string, PresenceSummary>> {
  if (!ids.length) return {}
  const res = await http.get<Paginated<PresenceBatchItem>>('/screens/presence', { query: { screen_id: ids.slice(0, 100) } })
  const out: Record<string, PresenceSummary> = {}
  for (const it of res.items ?? []) out[it.screen_id] = it
  return out
}

export function presenceToSummary(p: ScreenPresence): PresenceSummary {
  return {
    screen_id: p.screen_id,
    paired: p.paired,
    device_id: p.device?.id ?? null,
    status: p.live?.status ?? 'offline',
    player_state: p.live?.player_state ?? p.device?.player_state ?? null,
    last_seen_ms: p.live?.last_seen_ms ?? null,
    last_seen_at: p.device?.last_seen_at ?? null,
    app_version: p.live?.app_version ?? p.device?.app_version ?? null,
    manifest_version_applied: p.live?.manifest_version_applied ?? p.device?.applied_manifest_version ?? null,
    manifest_version_pending: p.live?.manifest_version_pending ?? p.device?.pending_manifest_version ?? null,
    download: p.live?.download ?? null,
  }
}

export const scheduleApi = {
  state: (id: string) => http.get<ScreenInventoryState>(`/inventory/screens/${id}/state`),
  anchorHistory: (id: string) => http.get<{ items: AnchorHistoryEntry[] }>(`/inventory/screens/${id}/anchor/history`),
  resetAnchor: (id: string, reason: string) =>
    http.post<{ screen_id: string; anchor_ms: number | null }>(`/inventory/screens/${id}/anchor/reset`, { reason }),
  grid: (id: string, hours: number) => http.get<ScreenGrid>(`/scheduler/screens/${id}/grid`, { query: { hours } }),
  /** Visão SEGURA para o estabelecimento (só código + tipo); `screens.view` ou `playlists.view`. */
  programming: (id: string, hours: number) =>
    http.get<ScreenProgramming>(`/scheduler/screens/${id}/programming`, { query: { hours } }),
  manifests: (id: string) => list<ManifestVersionMeta>(`/scheduler/screens/${id}/manifests`, { page_size: 10 }),
  regenerate: (id: string) =>
    http.post<{ screen_id: string; version: number | null }>(`/scheduler/screens/${id}/manifests/regenerate`, { force: false }),
  emergencies: (active = false) => http.get<{ items: Emergency[] }>('/scheduler/emergencies', { query: { active } }),
  createEmergency: (body: Body) =>
    http.post<Emergency>('/scheduler/emergencies', body, { idempotencyKey: newRequestId() }),
  cancelEmergency: (id: string, reason: string) => http.post<Emergency>(`/scheduler/emergencies/${id}/cancel`, { reason }),
}

// --------------------------------------------------------------------------- Conteúdo interno
export const contentApi = {
  internal: (establishment_id: string, screen_id?: string | null) =>
    http.get<Paginated<InternalItem>>('/internal-content', { query: { establishment_id, screen_id: screen_id ?? undefined } }),
  addInternal: (body: Body) => http.post<InternalItem>('/internal-content', body),
  patchInternal: (id: string, active: boolean) => http.patch<InternalItem>(`/internal-content/${id}`, { active }),
  removeInternal: (id: string) => http.delete<void>(`/internal-content/${id}`),
  reorder: (body: { establishment_id: string; screen_id?: string | null; item_ids: string[] }) =>
    http.put<{ items: InternalItem[] }>('/internal-content/order', body),
  fallback: (q: QueryParams = {}) => http.get<{ items: FallbackItem[] }>('/internal-content/fallback', { query: q }),
  addFallback: (body: Body) => http.post<FallbackItem>('/internal-content/fallback', body),
  removeFallback: (id: string) => http.delete<void>(`/internal-content/fallback/${id}`),
}

export const creativesApi = {
  list: (q: QueryParams) => list<CreativeLite>('/creatives', q),
  get: (id: string) => http.get<CreativeLite>(`/creatives/${id}`),
  /** Peça da RD (INTERNAL/FALLBACK/INSTITUTIONAL) — nasce RASCUNHO; só equipe RD global. */
  create: (body: { org_id: string; name: string; media_file_id: string; duration_seconds: number; kind: RdPieceKind }) =>
    http.post<CreativeLite>('/creatives', body),
  /** Revisão de conteúdo RD (não-EXTERNAL): campaigns.approve GLOBAL; rejeitar exige motivo (≥ 10). */
  review: (id: string, body: { decision: 'APPROVE' | 'REJECT'; reason?: string | null }) =>
    http.post<CreativeLite>(`/creatives/${id}/review`, body),
}

export const mediaLiteApi = {
  ready: (q: string) => list<MediaLite>('/media', { status: 'READY', q: q || undefined, page_size: 25 }),
}

// --------------------------------------------------------------------------- Planos / comodato / contratos
export const plansApi = {
  list: (include_inactive = false) => http.get<Paginated<PartnerPlan>>('/partner-plans', { query: { include_inactive } }),
  create: (body: Body) => http.post<PartnerPlan>('/partner-plans', body),
  update: (id: string, body: Body) => http.patch<PartnerPlan>(`/partner-plans/${id}`, body),
}

export const comodatoApi = {
  list: (q: QueryParams) => list<ComodatoAgreement>('/comodato-agreements', q),
  get: (id: string) => http.get<ComodatoAgreement>(`/comodato-agreements/${id}`),
  create: (body: Body) => http.post<ComodatoAgreement>('/comodato-agreements', body),
  end: (id: string, reason: string) => http.post<ComodatoAgreement>(`/comodato-agreements/${id}/end`, { reason }),
}

export const contractsApi = {
  templates: (q: QueryParams = {}) => http.get<Paginated<ContractTemplate>>('/contracts/templates', { query: q }),
  template: (id: string) => http.get<ContractTemplate>(`/contracts/templates/${id}`),
  current: (kind: string) => http.get<ContractTemplate>('/contracts/templates/current', { query: { kind } }),
  createTemplate: (body: Body) => http.post<ContractTemplate>('/contracts/templates', body),
  updateTemplate: (id: string, body: Body) => http.patch<ContractTemplate>(`/contracts/templates/${id}`, body),
  publish: (id: string) => http.post<ContractTemplate>(`/contracts/templates/${id}/publish`),
  accept: (body: Body) => http.post<ContractAcceptance>('/contracts/accept', body, { idempotencyKey: newRequestId() }),
  manual: (body: Body) => http.post<ContractAcceptance>('/contracts/acceptances/manual', body),
  acceptances: (q: QueryParams) => list<ContractAcceptance>('/contracts/acceptances', q),
  /** PDF do aceite (download autenticado). */
  pdf: (id: string) => fetchBlob(`/contracts/acceptances/${id}/pdf`),
}

// --------------------------------------------------------------------------- Onboarding
export const onboardingApi = {
  list: (q: QueryParams) => list<OnboardingApplication>('/onboarding', q),
  get: (id: string) => http.get<OnboardingApplication>(`/onboarding/${id}`),
  create: (body: { email: string; org_id?: string | null }) => http.post<OnboardingApplication>('/onboarding', body),
  invite: (email: string) => http.post<OnboardingApplication>('/onboarding/invites', { email }),
  saveStep: (id: string, step: string, body: Body) => http.put<OnboardingApplication>(`/onboarding/${id}/steps/${step}`, body),
  submit: (id: string) => http.post<OnboardingApplication>(`/onboarding/${id}/submit`),
  approve: (id: string) => http.post<OnboardingApplication>(`/onboarding/${id}/approve`, undefined, { idempotencyKey: newRequestId() }),
  reject: (id: string, reason: string) => http.post<OnboardingApplication>(`/onboarding/${id}/reject`, { reason }),
  uploadDocument: (id: string, kind: string, file: File, onProgress?: (f: number) => void) => {
    const form = new FormData()
    form.append('kind', kind)
    form.append('file', file)
    return uploadWithProgress<OnboardingDocument>({ path: `/onboarding/${id}/documents`, form, onProgress })
  },
}

export const ONBOARDING_TOKEN_HEADER = 'X-RD-Onboarding-Token'

/** Rotas públicas do auto-cadastro: token SEMPRE no header (nunca na URL da API); sem Bearer. */
export const publicOnboardingApi = {
  signup: (email: string, captcha_token?: string) =>
    request<{ status: string; message: string }>('/onboarding/public/signup', {
      method: 'POST',
      body: { email, captcha_token },
      auth: false,
      retryOn401: false,
      sendContext: false,
    }),
  get: (token: string) =>
    request<OnboardingApplication>('/onboarding/public/application', {
      headers: { [ONBOARDING_TOKEN_HEADER]: token },
      auth: false,
      retryOn401: false,
      sendContext: false,
    }),
  saveStep: (token: string, step: string, body: Body) =>
    request<OnboardingApplication>(`/onboarding/public/application/steps/${step}`, {
      method: 'PUT',
      body,
      headers: { [ONBOARDING_TOKEN_HEADER]: token },
      auth: false,
      retryOn401: false,
      sendContext: false,
    }),
  submit: (token: string) =>
    request<OnboardingApplication>('/onboarding/public/application/submit', {
      method: 'POST',
      headers: { [ONBOARDING_TOKEN_HEADER]: token },
      auth: false,
      retryOn401: false,
      sendContext: false,
    }),
  uploadDocument: (token: string, kind: string, file: File) => {
    const form = new FormData()
    form.append('kind', kind)
    form.append('file', file)
    return request<OnboardingDocument>('/onboarding/public/application/documents', {
      method: 'POST',
      body: form,
      headers: { [ONBOARDING_TOKEN_HEADER]: token },
      auth: false,
      retryOn401: false,
      sendContext: false,
    })
  },
}

// --------------------------------------------------------------------------- Estoque / manutenção / instalação
export const equipmentApi = {
  list: (q: QueryParams) => list<EquipmentItem>('/equipment', q),
  summary: () => http.get<EquipmentSummary>('/equipment/summary'),
  get: (id: string) => http.get<EquipmentItem>(`/equipment/${id}`),
  create: (body: Body) => http.post<EquipmentItem>('/equipment', body),
  update: (id: string, body: Body) => http.patch<EquipmentItem>(`/equipment/${id}`, body),
  movements: (id: string, page = 1) => list<EquipmentMovement>(`/equipment/${id}/movements`, { page, page_size: 25 }),
  move: (id: string, body: Body) =>
    http.post<{ movement: EquipmentMovement; item: EquipmentItem }>(`/equipment/${id}/movements`, body, {
      idempotencyKey: newRequestId(),
    }),
  ofScreen: (screenId: string) => http.get<Paginated<EquipmentItem>>(`/screens/${screenId}/equipment`),
}

export const maintenanceApi = {
  list: (q: QueryParams) => list<MaintenanceOrder>('/maintenance', q),
  get: (id: string) => http.get<MaintenanceOrder>(`/maintenance/${id}`),
  open: (body: Body) => http.post<MaintenanceOrder>('/maintenance', body),
  assign: (id: string, technician_user_id: string) => http.post<MaintenanceOrder>(`/maintenance/${id}/assign`, { technician_user_id }),
  status: (id: string, body: { status: string; note?: string | null }) => http.post<MaintenanceOrder>(`/maintenance/${id}/status`, body),
  note: (id: string, note: string) => http.post<MaintenanceOrder>(`/maintenance/${id}/notes`, { note }),
}

export const installationsApi = {
  list: (q: QueryParams) => list<Installation>('/installations', q),
  get: (id: string) => http.get<Installation>(`/installations/${id}`),
  create: (body: Body) => http.post<Installation>('/installations', body),
  start: (id: string) => http.post<Installation>(`/installations/${id}/start`),
  checklist: (id: string, items: Array<{ key: string; done: boolean; note?: string | null }>) =>
    http.put<Installation>(`/installations/${id}/checklist`, { items }),
  geolocation: (id: string, body: { lat: number; lng: number; accuracy_m?: number | null }) =>
    http.put<Installation>(`/installations/${id}/geolocation`, body),
  connectionTest: (id: string) =>
    http.post<{ result: Installation['connection_test']; installation: Installation }>(`/installations/${id}/connection-test`),
  photo: (id: string, kind: string, file: File, onProgress?: (f: number) => void) => {
    const form = new FormData()
    form.append('kind', kind)
    form.append('file', file)
    return uploadWithProgress<InstallationPhoto>({ path: `/installations/${id}/photos`, form, onProgress })
  },
  photoBlob: (id: string, photoId: string) => fetchBlob(`/installations/${id}/photos/${photoId}`),
  signature: (id: string, signer_name: string, signer_role: string | null, file: Blob) => {
    const form = new FormData()
    form.append('signer_name', signer_name)
    if (signer_role) form.append('signer_role', signer_role)
    form.append('file', file, 'assinatura.png')
    return uploadWithProgress<Installation>({ path: `/installations/${id}/signature`, form })
  },
  complete: (id: string, connection_test_waiver?: string | null) =>
    http.post<Installation>(`/installations/${id}/complete`, { connection_test_waiver: connection_test_waiver || null }),
  cancel: (id: string, reason: string) => http.post<Installation>(`/installations/${id}/cancel`, { reason }),
  /** Planejamento antes da conclusão (W-ORG §16): só os campos alterados; `screen_id: null` desvincula. */
  update: (
    id: string,
    body: { screen_id?: string | null; technician_user_id?: string; scheduled_date?: string | null; notes?: string | null },
  ) => http.patch<Installation>(`/installations/${id}`, body),
}

// --------------------------------------------------------------------------- Usuários (técnicos) / auditoria
export const usersApi = {
  get: (id: string) => http.get<UserLite>(`/users/${id}`),
}

/** Resposta de `GET /users/assignable` (W1): somente id e nome, sem exigir users.view. */
export interface AssignableUsers {
  items: Array<{ id: string; name: string }>
  total: number
  truncated: boolean
  permission: string
}

/**
 * Pessoas que podem RECEBER um trabalho que exige `permission` (técnicos). Com `establishment_id`,
 * o servidor devolve só quem atende aquele estabelecimento. O ator precisa ter a própria permissão.
 */
export function assignableUsers(permission: 'maintenance.manage' | 'equipment.manage', establishmentId?: string | null) {
  return http.get<AssignableUsers>('/users/assignable', {
    query: { permission, establishment_id: establishmentId ?? undefined },
  })
}

export const auditApi = {
  list: (q: QueryParams) => list<AuditRow>('/audit', q),
}

// --------------------------------------------------------------------------- Download autenticado

/** Baixa um binário autenticado (PDF/foto) com o Bearer em memória — nunca token em URL. */
export async function fetchBlob(path: string): Promise<{ blob: Blob; filename: string | null }> {
  const attempt = async () => {
    const headers: Record<string, string> = { 'X-Request-ID': newRequestId() }
    const t = tokenStore.get()
    if (t) headers.Authorization = `Bearer ${t}`
    const ctx = getActiveContextHeader()
    if (ctx) headers['X-RD-Context'] = ctx
    return fetch(buildUrl(path), { headers, credentials: 'omit' })
  }
  let res = await attempt()
  if (res.status === 401 && (await refreshAccessToken())) res = await attempt()
  if (!res.ok) {
    let body: unknown
    try {
      body = await res.json()
    } catch {
      body = undefined
    }
    throw toApiError(res.status, body, { requestId: res.headers.get('x-request-id') ?? undefined })
  }
  const cd = res.headers.get('content-disposition') ?? ''
  const m = /filename="?([^";]+)"?/i.exec(cd)
  return { blob: await res.blob(), filename: m ? m[1] : null }
}

/** Dispara o download de um Blob (sem abrir nova aba). */
export function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
