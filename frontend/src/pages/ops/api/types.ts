/**
 * Tipos das respostas do backend usadas pelas páginas de Operação (FE-OPS).
 * Fonte: código dos endpoints W-ORG / W-PLAY / W-INV (OpenAPI gerado em stack/logs/002/openapi_FE-OPS.json).
 * Campos marcados como opcionais (`?`) podem não existir na versão atual do servidor.
 */

export type Region = 'NORTE' | 'NORDESTE' | 'CENTRO_OESTE' | 'SUDESTE' | 'SUL'
export type DocumentType = 'CPF' | 'CNPJ'
export type VenueClass = 'STANDARD' | 'PREMIUM'
export type AudienceMethod = 'MANUAL' | 'FLOW_ESTIMATE'

export interface AudienceLabel {
  value: number
  is_estimate: true
  method: AudienceMethod | string | null
  label: string
}

export interface Establishment {
  id: string
  org_id: string
  name: string
  trade_name: string | null
  legal_name: string | null
  document: string | null
  document_type: DocumentType | null
  responsible_name: string | null
  phone: string | null
  email: string | null
  address: string | null
  number: string | null
  complement: string | null
  neighborhood: string | null
  city: string
  city_key: string
  city_ibge_code: string | null
  state: string
  region: Region
  region_label: string | null
  country: string
  postal_code: string | null
  lat: number | null
  lng: number | null
  geo_source: 'MANUAL' | 'GPS' | null
  geo_accuracy_m: number | null
  geo_captured_at: string | null
  timezone: string
  timezone_source: string | null
  category_id: string | null
  venue_class: VenueClass | null
  weight_milli: number | null
  estimated_daily_visitors: AudienceLabel | null
  partner_plan_id: string | null
  receives_commission: boolean
  pays_monthly_fee: boolean
  monthly_fee_cents: number | null
  onboarding_status: 'AWAITING_CONTRACT' | 'ACTIVE' | string
  responsible_technician_id: string | null
  installation_date: string | null
  is_active: boolean
  deactivated_at: string | null
  deactivation_reason: string | null
  operational_config: Record<string, unknown> | null
  notes: string | null
  created_at: string | null
  updated_at: string | null
  segment_ids?: string[]
  product_ids?: string[]
  screens_count?: number
  screens_sellable?: number
}

export interface TimezoneSuggestion {
  timezone: string
  reason: string
  confidence: 'CITY' | 'STATE'
}

export interface AudienceEstimate {
  id: string
  daily_people: number | null
  is_estimate: true
  method: AudienceMethod | string
  basis?: Record<string, unknown> | null
  note?: string | null
  created_at: string | null
  label?: string
}

export interface TaxonomyNode {
  id: string
  parent_id: string | null
  name: string
  slug: string
  depth: number
  is_active: boolean
  ancestor_ids: string[]
  kind?: 'SECTOR' | 'CATEGORY' | 'SUBCATEGORY'
  policy?: string | null
}

export interface Product {
  id: string
  name: string
  slug: string
  segment_id: string
  brand_id: string | null
  is_active: boolean
}

// --------------------------------------------------------------------------- Telas
export type ScreenStatus = 'ACTIVE' | 'MAINTENANCE' | 'INACTIVE' | 'DECOMMISSIONED'
export type Orientation = 'HORIZONTAL' | 'VERTICAL'

export interface ScreenEstablishmentRef {
  id: string
  name: string
  org_id: string
  city: string
  state: string
  city_key: string
  timezone: string
  is_active: boolean
  onboarding_status: string
}

export interface ScreenHardware {
  brand: string | null
  model: string | null
  inches: number | null
  resolution_w: number | null
  resolution_h: number | null
  serial_number: string | null
  asset_tag: string | null
  player_brand: string | null
  player_model: string | null
  player_serial_number: string | null
  installed_at: string | null
  installed_by: string | null
}

export interface Screen {
  id: string
  friendly_id: string
  name: string
  establishment_id: string
  status: ScreenStatus
  sellable: boolean
  decommissioned_at: string | null
  decommission_reason: string | null
  display_type: string
  player_type: string
  orientation: Orientation
  hardware: ScreenHardware
  tv_owner: 'RD' | 'ESTABLISHMENT'
  device_id: string | null
  screen_class: VenueClass
  weight_milli: number
  programming_mode: 'ESTABLISHMENT_DEFAULT' | 'CUSTOM'
  estimated_daily_audience: AudienceLabel | null
  notes: string | null
  created_at: string | null
  updated_at: string | null
  establishment?: ScreenEstablishmentRef
  equipment?: EquipmentItem[] | null
}

export interface ScreenHistoryEntry {
  id: string
  event: string
  establishment_id: string
  from_establishment_id: string | null
  before: Record<string, unknown> | null
  after: Record<string, unknown> | null
  reason: string | null
  actor_user_id: string | null
  created_at: string | null
}

/** Estados exibidos de presença (W-PLAY presence.DISPLAY_STATUSES). */
export type PresenceStatus =
  | 'conectado'
  | 'online'
  | 'offline'
  | 'sincronizando'
  | 'sincronizado'
  | 'reproduzindo'
  | 'travado'
  | 'erro'

export interface DownloadProgress {
  done_bytes: number | null
  total_bytes: number | null
}

export interface PresenceLive {
  status: PresenceStatus | string
  player_state?: string | null
  manifest_version_applied?: number | null
  manifest_version_pending?: number | null
  sync?: string | null
  last_seen_ms?: number | null
  last_sync_ms?: number | null
  last_play_ms?: number | null
  current_code?: string | null
  clock_offset_ms?: number | null
  app_version?: string | null
  download?: DownloadProgress | null
}

export interface Telemetry {
  cpu_pct?: number | null
  ram_used_mb?: number | null
  ram_total_mb?: number | null
  disk_free_mb?: number | null
  disk_total_mb?: number | null
  uptime_s?: number | null
  os?: string | null
  app_version?: string | null
  platform?: string | null
  resolution?: string | null
  orientation?: string | null
  wifi?: { ssid: string | null; rssi_dbm: number | null } | null
  hdmi_connected?: boolean | null
}

export interface Device {
  id: string
  screen_id: string
  platform: string | null
  app_version: string | null
  os_version: string | null
  device_info: Record<string, unknown> | null
  update_group: string | null
  status: 'ACTIVE' | 'REVOKED' | string
  key_fingerprint: string | null
  paired_at: string | null
  pairing_method: string | null
  revoked_at: string | null
  revoked_reason: string | null
  presence: 'online' | 'offline' | string | null
  player_state: string | null
  last_seen_at: string | null
  last_sync_at: string | null
  last_play_at: string | null
  applied_manifest_version: number | null
  pending_manifest_version: number | null
  last_error: string | null
  telemetry: Telemetry | null
  telemetry_at: string | null
  live?: PresenceLive
}

export interface ScreenPresence {
  screen_id: string
  screen_code: string | null
  paired: boolean
  device: Device | null
  live: PresenceLive
}

/**
 * Resumo de presença por tela — item de `GET /screens/presence` (lote) ou derivado de
 * `GET /screens/{id}/presence`; `last_seen_ms` vem do evento `screen.status`.
 */
export interface PresenceSummary {
  screen_id?: string
  paired: boolean
  device_id?: string | null
  status: PresenceStatus | string
  player_state?: string | null
  last_seen_at?: string | null
  last_seen_ms?: number | null
  app_version?: string | null
  manifest_version_applied?: number | null
  manifest_version_pending?: number | null
  download?: DownloadProgress | null
}

export type CommandName = 'restart_app' | 'resync' | 'clear_cache' | 'redownload' | 'update_app' | 'reboot' | 'reload_manifest'

export interface DeviceCommand {
  id: string
  device_id: string
  screen_id: string
  command: string
  args: Record<string, unknown> | null
  reason: string
  confirmed: boolean
  issued_by_user_id: string | null
  /** Nome do autor resolvido pelo servidor (W-PLAY pós-janela 3); `null` = sem autor/usuário removido. */
  issued_by_name?: string | null
  status: string
  created_at: string | null
  expires_at: string | null
  delivered_at: string | null
  delivery_count: number
  acked_at: string | null
  ack_status: string | null
  result_detail: string | null
}

export interface ManifestVersionMeta {
  screen_id: string
  version: number
  manifest_id: string
  generated_at: string | null
  reason: string | null
  size_bytes: number | null
  horizon_from: string | null
  horizon_to: string | null
  unplaced_count: number
  applied_at: string | null
  applied_by_device: string | null
  content_available: boolean
}

export interface RotationSummary {
  internal_seconds: number
  external_seconds: number
  cycle_seconds: number
  cycles_per_hour: number
  units_per_block: number
  units_per_hour: number
  policy_id?: string | null
  scope?: string
}

export interface ScreenInventoryState {
  screen_id: string
  screen_code: string
  establishment_id: string
  timezone: string
  local_date: string
  policy: RotationSummary
  anchor_ms: number | null
  anchor_source: string | null
  anchor_reported_at: string | null
  sellable: boolean
  unsellable_reason: string | null
  inventory_version: number
  manifest_version: number
  manifest_generated_at: string | null
  manifest_applied_version: number | null
  manifest_unplaced: number
}

export interface AnchorHistoryEntry {
  id: string
  old_anchor_ms: number | null
  new_anchor_ms: number | null
  source: string
  reason: string | null
  actor_user_id: string | null
  device_id: string | null
  created_at: string | null
}

export interface GridEntry {
  u: number
  d: number
  code: string
  kind: string
  campaign_id?: string | null
  creative_id?: string | null
}

export interface GridBlock {
  bucket_ms: number
  entries: GridEntry[]
}

export interface ScreenGrid {
  screen_id: string
  timezone: string
  rotation: { cycle_ms: number; internal_ms?: number; external_ms?: number; [k: string]: unknown }
  anchor_ms: number | null
  blocks: GridBlock[]
  internal_pattern?: unknown
  emergency?: unknown
  unplaced?: unknown[]
}

/** `GET /scheduler/screens/{id}/programming` (W-INV) — visão segura: entradas só `{code, kind}`. */
export interface ProgrammingInternalPiece {
  code: string
  name: string | null
  duration_ms: number
}

export interface ScreenProgramming {
  screen_id: string
  screen_code: string
  timezone: string
  rotation: { internal_ms: number; external_ms: number; cycle_ms: number; unit_ms: number }
  anchor_ms: number | null
  from_ms: number
  to_ms: number
  blocks: Array<{ bucket_ms: number; entries: Array<{ code: string; kind: string }> }>
  /** Um item por bloco interno do ciclo; cada um é a sequência de peças do estabelecimento. */
  internal_pattern: ProgrammingInternalPiece[][]
}

// --------------------------------------------------------------------------- Comercial do parceiro
export type TvProvision = 'OWN_TV' | 'RD_TV_COMODATO'
export type UpgradePolicy = 'FREE' | 'PAID' | 'NEGOTIATED'

export interface PartnerPlan {
  id: string
  name: string
  slug: string
  description: string | null
  monthly_fee_cents: number
  is_free: boolean
  tv_provision: TvProvision
  default_tv_inches: number | null
  deposit_required: boolean
  deposit_cents: number
  installation_fee_required: boolean
  installation_fee_cents: number
  upgrade_policy: UpgradePolicy
  upgrade_fee_cents: number | null
  receives_commission: boolean
  default_internal_s: number | null
  default_external_s: number | null
  allow_external_overflow: boolean
  is_active: boolean
  created_at: string | null
  updated_at: string | null
}

export interface ComodatoAgreement {
  id: string
  org_id: string
  establishment_id: string
  screen_id: string | null
  partner_plan_id: string | null
  deposit_required: boolean
  deposit_cents: number
  installation_fee_required: boolean
  installation_fee_cents: number
  upgrade_policy: UpgradePolicy
  upgrade_fee_cents: number | null
  maintenance_paid_by: 'RD' | 'ESTABLISHMENT'
  breakage_paid_by: 'RD' | 'ESTABLISHMENT'
  status: 'ACTIVE' | 'ENDED'
  started_at: string | null
  ended_at: string | null
  end_reason: string | null
  notes: string | null
  items?: EquipmentItem[]
  /** Presente na lista e no detalhe (não no retorno da criação). */
  establishment_name?: string | null
}

export type ContractKind = 'PARTNERSHIP' | 'ADVERTISER_TERMS' | 'COMODATO'

export interface ContractTemplate {
  id: string
  kind: ContractKind
  kind_label: string | null
  version: number
  title: string
  body_sha256: string | null
  status: 'DRAFT' | 'PUBLISHED' | 'RETIRED'
  published_at: string | null
  retired_at: string | null
  created_at: string | null
  body?: string
}

export interface ContractAcceptance {
  id: string
  template_id: string
  kind: ContractKind
  version: number
  body_sha256: string | null
  org_id: string
  establishment_id: string | null
  comodato_agreement_id: string | null
  method: string
  accepted_by_user_id: string | null
  recorded_by_user_id: string | null
  signer_name: string | null
  ip_address: string | null
  accepted_at: string | null
  pdf: { sha256: string | null; size: number | null } | null
  notes: string | null
  created?: boolean
  establishment_activated?: string | null
}

// --------------------------------------------------------------------------- Onboarding
export type OnboardingStatus = 'INVITED' | 'IN_PROGRESS' | 'SUBMITTED' | 'APPROVED' | 'REJECTED' | 'CANCELLED'
export type OnboardingStepKey = 'company' | 'establishment' | 'address' | 'screens' | 'commercial' | 'installation'

export interface OnboardingStepInfo {
  key: OnboardingStepKey
  required: boolean
  completed: boolean
  staff_only: boolean
}

export interface OnboardingDocument {
  id: string
  kind: string
  content_type: string
  size_bytes: number
  sha256: string
  created_at?: string | null
}

export interface OnboardingApplication {
  id: string
  origin: 'ADMIN' | 'INVITE' | 'SELF_SIGNUP' | string
  status: OnboardingStatus
  email: string
  steps: OnboardingStepInfo[]
  data: Partial<Record<OnboardingStepKey, Record<string, unknown>>>
  submitted_at: string | null
  decided_at: string | null
  decision_reason: string | null
  org_id: string | null
  establishment_id: string | null
  created_at: string | null
  token_expires_at?: string | null
  invited_by?: string | null
  decided_by?: string | null
  documents?: OnboardingDocument[]
  next_steps?: string[]
}

// --------------------------------------------------------------------------- Estoque
export type EquipmentType = 'TV' | 'TVBOX' | 'CABLE' | 'REMOTE' | 'MOUNT' | 'ACCESSORY'
export type LocationKind = 'EXTERNAL' | 'WAREHOUSE' | 'TECHNICIAN' | 'ESTABLISHMENT' | 'SCREEN' | 'MAINTENANCE' | 'RMA' | 'DISPOSED'
export type MovementType =
  | 'ENTRY'
  | 'TRANSFER'
  | 'INSTALL'
  | 'LOAN'
  | 'COMODATO'
  | 'RETURN'
  | 'MAINTENANCE_IN'
  | 'MAINTENANCE_OUT'
  | 'RMA_OUT'
  | 'RMA_RETURN'
  | 'WRITE_OFF'
  | 'ADJUSTMENT'

export interface ItemBalance {
  kind: LocationKind
  ref_id: string | null
  city_key: string | null
  quantity: number
}

export interface EquipmentItem {
  id: string
  type: EquipmentType
  tracking: 'SERIAL' | 'BULK'
  name: string
  brand: string | null
  model: string | null
  serial_number: string | null
  asset_tag: string | null
  inches: number | null
  unit: string
  condition: 'NEW' | 'GOOD' | 'DAMAGED' | 'BROKEN'
  supplier: string | null
  purchase_date: string | null
  purchase_cost_cents: number | null
  warranty_until: string | null
  notes: string | null
  status: string
  location: {
    kind: LocationKind
    city_key: string | null
    establishment_id: string | null
    screen_id: string | null
    user_id: string | null
  } | null
  comodato_agreement_id: string | null
  expected_return_at: string | null
  movement_count: number
  last_movement_at: string | null
  created_at: string | null
  balances?: ItemBalance[]
  total_quantity?: number
}

export interface EquipmentMovement {
  id: string
  seq: number
  item_id: string
  group_id: string | null
  movement_type: MovementType
  quantity: number
  from: { kind: LocationKind; ref_id: string | null; city_key: string | null }
  to: { kind: LocationKind; ref_id: string | null; city_key: string | null }
  from_status: string | null
  to_status: string | null
  establishment_id: string | null
  screen_id: string | null
  comodato_agreement_id: string | null
  replaced_item_id: string | null
  reference: string | null
  expected_return_at: string | null
  reason: string | null
  performed_by: string | null
  created_at: string | null
}

export interface EquipmentSummary {
  serial_by_type_status: Record<string, Record<string, number>>
  bulk_in_warehouse: Array<{ type: EquipmentType; name: string; quantity: number }>
}

// --------------------------------------------------------------------------- Manutenção / instalação
export type MaintenanceStatus = 'OPEN' | 'IN_PROGRESS' | 'WAITING_PARTS' | 'RESOLVED' | 'CANCELLED'

export interface MaintenanceEvent {
  id: string
  event: string
  from_status: string | null
  to_status: string | null
  technician_user_id: string | null
  note: string | null
  actor_user_id: string | null
  /** Nomes resolvidos pelo servidor (W-ORG §16 B2). */
  actor_name?: string | null
  technician_name?: string | null
  created_at: string | null
}

export interface MaintenanceOrder {
  id: string
  establishment_id: string
  screen_id: string | null
  equipment_item_id: string | null
  component: 'TV' | 'TVBOX' | 'CABLE' | 'NETWORK' | 'OTHER'
  status: MaintenanceStatus
  priority: 'LOW' | 'NORMAL' | 'HIGH' | 'URGENT'
  description: string
  resolution: string | null
  technician_user_id: string | null
  opened_by: string | null
  opened_by_name?: string | null
  opened_at: string | null
  closed_at: string | null
  history?: MaintenanceEvent[]
  establishment_name: string | null
  screen_code: string | null
  technician_name: string | null
}

export interface ChecklistItem {
  key: string
  label: string
  required: boolean
  done: boolean
  note: string | null
}

export interface ConnectionTest {
  status: 'ONLINE' | 'OFFLINE' | 'NOT_PAIRED' | 'UNAVAILABLE'
  checked_at: string
  source: string
  device_id?: string
  last_seen_age_ms?: number | null
  player_status?: string | null
  app_version?: string | null
  detail?: string
}

export interface InstallationPhoto {
  id: string
  kind: 'TV_WORKING' | 'EQUIPMENT' | 'SITE' | 'OTHER'
  content_type: string
  size_bytes: number
  sha256: string
  width: number | null
  height: number | null
  created_at?: string | null
  uploaded_by?: string | null
  uploaded_by_name?: string | null
}

export interface Installation {
  id: string
  establishment_id: string
  screen_id: string | null
  technician_user_id: string
  status: 'SCHEDULED' | 'IN_PROGRESS' | 'COMPLETED' | 'CANCELLED'
  scheduled_date: string | null
  started_at: string | null
  completed_at: string | null
  checklist: ChecklistItem[]
  geolocation: {
    lat: number
    lng: number
    accuracy_m: number | null
    captured_at: string | null
    distance_to_establishment_m: number | null
  } | null
  connection_test: ConnectionTest | null
  connection_test_waiver: string | null
  signer: { name: string; role: string | null; image_sha256: string | null; signed_at: string | null } | null
  notes: string | null
  created_at: string | null
  created_by?: string | null
  created_by_name?: string | null
  photos?: InstallationPhoto[]
  establishment_name: string | null
  screen_code: string | null
  technician_name: string | null
}

// --------------------------------------------------------------------------- Conteúdo interno
export interface InternalItem {
  id: string
  establishment_id: string
  screen_id: string | null
  creative_id: string
  position: number
  active: boolean
  created_at: string | null
  updated_at: string | null
  code?: string
  name?: string
  duration_seconds?: number | null
}

export interface FallbackItem {
  id: string
  scope: 'GLOBAL' | 'ESTABLISHMENT'
  establishment_id: string | null
  creative_id: string
  purpose: 'ESTABLISHMENT_LOGO' | 'RD_DEFAULT' | 'RD_PROMO' | 'INSTITUTIONAL_MESSAGE'
  position: number
  active: boolean
  created_at: string | null
}

export interface Emergency {
  id: string
  title: string
  creative_id: string
  starts_at: string
  ends_at: string
  target_kind: 'ALL' | 'SCREENS' | 'ESTABLISHMENTS' | 'STATES' | 'CITIES'
  target_ids: string[]
  status: 'ACTIVE' | 'CANCELLED' | string
  reason: string
  created_by: string
  created_at: string | null
  cancelled_at: string | null
  cancel_reason: string | null
  screens?: number
}

export interface CreativeLite {
  id: string
  code: string
  org_id: string
  name: string
  kind: 'EXTERNAL' | 'INTERNAL' | 'INSTITUTIONAL' | 'FALLBACK'
  duration_seconds: number | null
  orientation: string | null
  status: string
  technical_status: string | null
  technical_error?: string | null
  rejection_reason?: string | null
  media_duration_ms?: number | null
  created_at?: string
}

export type RdPieceKind = 'INTERNAL' | 'FALLBACK' | 'INSTITUTIONAL'

/** Recorte da mídia da biblioteca usado para criar peças da RD (GET /media?status=READY). */
export interface MediaLite {
  id: string
  org_id: string
  name: string
  kind: 'IMAGE' | 'VIDEO'
  status: string
  source: { duration_ms: number | null }
  output: { duration_ms: number | null; orientation: string } | null
}

export interface UserLite {
  id: string
  name: string
  email?: string
  is_active?: boolean
  assignments?: Array<{ role: string; scope_type: string; scope_id: string | null; scope_value: string | null }>
}

export interface AuditRow {
  id: string
  seq: number
  created_at: string
  actor_user_id: string | null
  real_user_id: string | null
  action: string
  entity_type: string
  entity_id: string
  before: Record<string, unknown> | null
  after: Record<string, unknown> | null
  reason: string | null
  origin: string | null
}

export interface Organization {
  id: string
  name: string
  trade_name?: string | null
  business_roles?: string[]
  is_active?: boolean
}
