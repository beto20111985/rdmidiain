/**
 * Tipos do RD Player Protocol v1 (protocol/RD_PLAYER_PROTOCOL_v1.md) + validação estrutural do manifesto.
 * Tudo em milissegundos inteiros (UTC). Nenhum float.
 */

export const PROTOCOL_PATHS = {
  pairingRequests: '/api/v1/player/pairing/requests',
  pairingRequest: (id: string) => `/api/v1/player/pairing/requests/${encodeURIComponent(id)}`,
  pairingClaim: (id: string) => `/api/v1/player/pairing/requests/${encodeURIComponent(id)}/claim`,
  pairingResult: (id: string) => `/api/v1/player/pairing/requests/${encodeURIComponent(id)}/result`,
  authChallenge: '/api/v1/player/auth/challenge',
  authToken: '/api/v1/player/auth/token',
  wsTicket: '/api/v1/player/ws-ticket',
  manifest: '/api/v1/player/manifest',
  anchor: '/api/v1/player/anchor',
  media: (mediaId: string) => `/api/v1/player/media/${encodeURIComponent(mediaId)}`,
  eventsBatch: '/api/v1/player/events:batch',
  appUpdate: '/api/v1/player/app-update',
  wsPlayer: '/ws/player',
  wsPairing: '/ws/player/pairing',
} as const;

export const PURPOSE_AUTH = 'rd-player-auth-v1';
export const PURPOSE_PAIRING_RESULT = 'rd-pairing-result-v1';
export const PURPOSE_APP_UPDATE = 'rd-app-update-v1';
export const HEADER_SERVER_TIME = 'X-Server-Time-Ms';
export const HEADER_DEVICE_SIGNATURE = 'X-RD-Device-Signature';
export const HEADER_DEVICE_TS = 'X-RD-Device-Ts-Ms';
export const ZERO_HASH = '0'.repeat(64);
export const MAX_EVENTS_PER_BATCH = 200;

export type Platform = 'web' | 'windows' | 'android';
export type CreativeKind = 'EXTERNAL' | 'INTERNAL' | 'INSTITUTIONAL' | 'FALLBACK';
export type Orientation = 'LANDSCAPE' | 'PORTRAIT';

// ───────────────────────── Pareamento / autenticação ─────────────────────────

export interface DeviceInfo {
  model: string;
  os: string;
  resolution: string;
}

export interface PairingRequestBody {
  public_key: string;
  key_alg: 'ES256';
  platform: Platform;
  app_version: string;
  device_info: DeviceInfo;
}

export interface PairingRequestResponse {
  pairing_request_id: string;
  pairing_code: string;
  expires_at_ms: number;
  panel_pair_url: string;
  ws_path: string;
}

export type PairingStatus = 'PENDING' | 'CLAIMED' | 'EXPIRED';

export interface PairingStatusResponse {
  status: PairingStatus;
  expires_at_ms: number;
}

export interface ServerKey {
  key_id: string;
  public_key: string;
  alg: 'ES256';
}

export interface PairingResult {
  device_id: string;
  screen_id: string;
  screen_code: string;
  server_keys: ServerKey[];
}

export interface ClaimOnDeviceBody {
  login: string;
  password: string;
  totp?: string;
  screen_code: string;
}

export interface ChallengeResponse {
  challenge: string;
  expires_at_ms: number;
}

export interface TokenResponse {
  access_token: string;
  expires_in: number;
  server_time_ms: number;
}

export interface WsTicketResponse {
  ticket: string;
  expires_in: number;
}

export interface ApiErrorBody {
  error: { code: string; message: string; details?: unknown };
}

// ───────────────────────── Manifesto ─────────────────────────

export interface Rotation {
  internal_ms: number;
  external_ms: number;
  cycle_ms: number;
  unit_ms: number;
  policy_id: string;
}

export interface Horizon {
  from_ms: number;
  to_ms: number;
  repeat_period_ms: number;
}

export interface FooterSettings {
  enabled: boolean;
  height_permille: number;
}

export interface ManifestSettings {
  image_default_ms: number;
  fade_ms: number;
  orientation: Orientation;
  footer: FooterSettings;
  offline_alert_days: number[];
  heartbeat_interval_ms: number;
  telemetry_interval_ms: number;
  panel_url: string | null;
}

export interface MediaEntry {
  media_id: string;
  sha256: string;
  size: number;
  mime: string;
  width: number;
  height: number;
  duration_ms: number | null;
}

export interface CreativeEntry {
  creative_id: string;
  code: string;
  kind: CreativeKind;
  campaign_id: string | null;
  advertiser_id: string | null;
  media: string;
  duration_ms: number;
  valid_from_ms: number;
  valid_until_ms: number;
  label: string;
}

export interface PatternItem {
  c: string;
  d: number;
}

export interface ExternalEntry {
  i: string;
  c: string;
  u: number;
  d: number;
}

export interface EmergencyEntry {
  id: string;
  c: string;
  from_ms: number;
  until_ms: number;
}

export interface Manifest {
  schema: 'rd.manifest/1';
  manifest_id: string;
  version: number;
  screen_id: string;
  screen_code: string;
  generated_at_ms: number;
  timezone: string;
  rotation: Rotation;
  anchor_ms: number | null;
  horizon: Horizon;
  settings: ManifestSettings;
  media: Record<string, MediaEntry>;
  creatives: Record<string, CreativeEntry>;
  internal_pattern: PatternItem[][];
  fallback: PatternItem[];
  external_plan: Record<string, ExternalEntry[]>;
  emergency: EmergencyEntry[];
  revoked: string[];
}

export interface SignedManifest {
  alg: 'ES256';
  key_id: string;
  signature: string;
  manifest: Manifest;
}

// ───────────────────────── PoP ─────────────────────────

export type PopResult = 'COMPLETED' | 'INTERRUPTED' | 'ERROR' | 'SKIPPED';

export interface PopEvent {
  schema: 'rd.pop/1';
  playback_uuid: string;
  device_id: string;
  screen_id: string;
  creative_id: string;
  creative_code: string;
  campaign_id: string | null;
  insertion_id: string | null;
  kind: CreativeKind;
  manifest_version: number;
  started_at_ms: number;
  finished_at_ms: number;
  expected_ms: number;
  actual_ms: number;
  result: PopResult;
  seq: number;
  nonce: string;
  prev_hash: string;
  clock_offset_ms: number;
  offline: boolean;
}

export interface SignedPopEvent {
  event: PopEvent;
  signature: string;
}

export type BatchStatus = 'ACCEPTED' | 'DUPLICATE' | 'REJECTED';

export interface BatchResult {
  playback_uuid: string;
  status: BatchStatus;
  valid: boolean;
  reason: string | null;
}

export interface BatchResponse {
  results: BatchResult[];
  ack_upto_seq: number | null;
}

// ───────────────────────── WebSocket ─────────────────────────

export interface Envelope<T = Record<string, unknown>> {
  type: string;
  id: string;
  ts_ms: number;
  data: T;
}

export type CommandName =
  | 'restart_app'
  | 'resync'
  | 'clear_cache'
  | 'redownload'
  | 'update_app'
  | 'reboot'
  | 'reload_manifest';

export const COMMAND_NAMES: readonly CommandName[] = [
  'restart_app',
  'resync',
  'clear_cache',
  'redownload',
  'update_app',
  'reboot',
  'reload_manifest',
];

export type AckStatus = 'RECEIVED' | 'DONE' | 'FAILED' | 'UNSUPPORTED';

export interface CommandData {
  command_id: string;
  command: CommandName | string;
  args: Record<string, unknown> | null;
  expires_at_ms: number | null;
}

export interface KillData {
  creative_ids: string[];
  reason: string | null;
}

export type PlayerState = 'PLAYING' | 'IDLE' | 'SYNCING' | 'ERROR' | 'FROZEN' | 'UNPAIRED';

export interface HeartbeatData {
  state: PlayerState;
  current: { creative_code: string; started_at_ms: number } | null;
  manifest_version_applied: number | null;
  manifest_version_pending: number | null;
  download: { done_bytes: number; total_bytes: number } | null;
  last_sync_ms: number | null;
  last_play_ms: number | null;
  storage: { free_bytes: number | null; used_bytes: number | null; persistent: boolean };
  clock_offset_ms: number;
  alerts: string[];
}

export interface TelemetryData {
  cpu_pct: number | null;
  ram_used_mb: number | null;
  ram_total_mb: number | null;
  disk_free_mb: number | null;
  disk_total_mb: number | null;
  uptime_s: number | null;
  os: string | null;
  app_version: string;
  platform: Platform;
  resolution: string | null;
  orientation: Orientation | null;
  wifi: { ssid: string | null; rssi_dbm: number | null } | null;
  hdmi_connected: boolean | null;
}

export interface AppUpdateInfo {
  version: string;
  url: string;
  sha256: string;
  size: number;
  signature: string;
  key_id?: string;
  mandatory: boolean;
}

// ───────────────────────── Validação estrutural ─────────────────────────

export class ProtocolError extends Error {
  constructor(
    public readonly code: string,
    message: string,
  ) {
    super(`${code}: ${message}`);
    this.name = 'ProtocolError';
  }
}

type Obj = Record<string, unknown>;

function isObj(v: unknown): v is Obj {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function req(o: Obj, key: string, path: string): unknown {
  if (!(key in o)) throw new ProtocolError('MANIFEST_INVALID', `chave ausente: ${path}.${key}`);
  return o[key];
}

function int(o: Obj, key: string, path: string, opts: { nullable?: boolean; min?: number } = {}): void {
  const v = req(o, key, path);
  if (v === null && opts.nullable) return;
  if (typeof v !== 'number' || !Number.isSafeInteger(v)) {
    throw new ProtocolError('MANIFEST_INVALID', `${path}.${key} deve ser inteiro`);
  }
  if (opts.min !== undefined && v < opts.min) throw new ProtocolError('MANIFEST_INVALID', `${path}.${key} < ${opts.min}`);
}

function str(o: Obj, key: string, path: string, nullable = false): void {
  const v = req(o, key, path);
  if (v === null && nullable) return;
  if (typeof v !== 'string') throw new ProtocolError('MANIFEST_INVALID', `${path}.${key} deve ser string`);
}

function obj(o: Obj, key: string, path: string): Obj {
  const v = req(o, key, path);
  if (!isObj(v)) throw new ProtocolError('MANIFEST_INVALID', `${path}.${key} deve ser objeto`);
  return v;
}

function arr(o: Obj, key: string, path: string): unknown[] {
  const v = req(o, key, path);
  if (!Array.isArray(v)) throw new ProtocolError('MANIFEST_INVALID', `${path}.${key} deve ser lista`);
  return v;
}

function patternItem(v: unknown, path: string): void {
  if (!isObj(v)) throw new ProtocolError('MANIFEST_INVALID', `${path} deve ser objeto`);
  str(v, 'c', path);
  int(v, 'd', path, { min: 1 });
}

const KINDS: readonly string[] = ['EXTERNAL', 'INTERNAL', 'INSTITUTIONAL', 'FALLBACK'];

/**
 * Valida a forma do manifesto (todas as chaves obrigatórias, inteiros, invariantes de rotação).
 * Referências pendentes (criativo/mídia inexistente) NÃO invalidam o manifesto: viram fallback na resolução.
 */
export function validateManifest(m: unknown): asserts m is Manifest {
  if (!isObj(m)) throw new ProtocolError('MANIFEST_INVALID', 'manifest deve ser objeto');
  const p = 'manifest';
  if (m.schema !== 'rd.manifest/1') throw new ProtocolError('MANIFEST_SCHEMA', `schema desconhecido: ${String(m.schema)}`);
  str(m, 'manifest_id', p);
  int(m, 'version', p, { min: 0 });
  str(m, 'screen_id', p);
  str(m, 'screen_code', p);
  int(m, 'generated_at_ms', p);
  str(m, 'timezone', p);
  const rot = obj(m, 'rotation', p);
  for (const k of ['internal_ms', 'external_ms', 'cycle_ms', 'unit_ms']) int(rot, k, `${p}.rotation`, { min: 0 });
  str(rot, 'policy_id', `${p}.rotation`, true);
  const I = rot.internal_ms as number;
  const E = rot.external_ms as number;
  const C = rot.cycle_ms as number;
  const U = rot.unit_ms as number;
  if (C <= 0 || U <= 0) throw new ProtocolError('MANIFEST_INVALID', 'cycle_ms/unit_ms devem ser > 0');
  if (I + E !== C) throw new ProtocolError('MANIFEST_INVALID', 'internal_ms + external_ms ≠ cycle_ms');
  if (3_600_000 % C !== 0) throw new ProtocolError('MANIFEST_INVALID', 'cycle_ms não divide 3 600 000');
  if (I % U !== 0 || E % U !== 0) throw new ProtocolError('MANIFEST_INVALID', 'internal/external não múltiplos de unit_ms');
  int(m, 'anchor_ms', p, { nullable: true });
  const hz = obj(m, 'horizon', p);
  int(hz, 'from_ms', `${p}.horizon`);
  int(hz, 'to_ms', `${p}.horizon`);
  int(hz, 'repeat_period_ms', `${p}.horizon`, { min: 0 });
  const st = obj(m, 'settings', p);
  const sp = `${p}.settings`;
  int(st, 'image_default_ms', sp, { min: 0 });
  int(st, 'fade_ms', sp, { min: 0 });
  str(st, 'orientation', sp);
  const footer = obj(st, 'footer', sp);
  if (typeof footer.enabled !== 'boolean') throw new ProtocolError('MANIFEST_INVALID', `${sp}.footer.enabled deve ser booleano`);
  int(footer, 'height_permille', `${sp}.footer`, { min: 0 });
  const days = arr(st, 'offline_alert_days', sp);
  days.forEach((d, i) => {
    if (typeof d !== 'number' || !Number.isSafeInteger(d)) throw new ProtocolError('MANIFEST_INVALID', `${sp}.offline_alert_days[${i}]`);
  });
  int(st, 'heartbeat_interval_ms', sp, { min: 1000 });
  int(st, 'telemetry_interval_ms', sp, { min: 1000 });
  str(st, 'panel_url', sp, true);

  const media = obj(m, 'media', p);
  for (const [key, v] of Object.entries(media)) {
    const mp = `${p}.media.${key}`;
    if (!isObj(v)) throw new ProtocolError('MANIFEST_INVALID', `${mp} deve ser objeto`);
    str(v, 'media_id', mp);
    str(v, 'sha256', mp);
    if (!/^[0-9a-f]{64}$/.test(v.sha256 as string)) throw new ProtocolError('MANIFEST_INVALID', `${mp}.sha256 inválido`);
    int(v, 'size', mp, { min: 0 });
    str(v, 'mime', mp);
    int(v, 'width', mp, { min: 0 });
    int(v, 'height', mp, { min: 0 });
    int(v, 'duration_ms', mp, { nullable: true, min: 0 });
  }
  const creatives = obj(m, 'creatives', p);
  for (const [key, v] of Object.entries(creatives)) {
    const cp = `${p}.creatives.${key}`;
    if (!isObj(v)) throw new ProtocolError('MANIFEST_INVALID', `${cp} deve ser objeto`);
    str(v, 'creative_id', cp);
    str(v, 'code', cp);
    str(v, 'kind', cp);
    if (!KINDS.includes(v.kind as string)) throw new ProtocolError('MANIFEST_INVALID', `${cp}.kind desconhecido`);
    str(v, 'campaign_id', cp, true);
    str(v, 'advertiser_id', cp, true);
    str(v, 'media', cp);
    int(v, 'duration_ms', cp, { min: 1 });
    int(v, 'valid_from_ms', cp);
    int(v, 'valid_until_ms', cp);
    str(v, 'label', cp);
  }
  arr(m, 'internal_pattern', p).forEach((seq, i) => {
    if (!Array.isArray(seq)) throw new ProtocolError('MANIFEST_INVALID', `${p}.internal_pattern[${i}] deve ser lista`);
    seq.forEach((it, j) => patternItem(it, `${p}.internal_pattern[${i}][${j}]`));
  });
  arr(m, 'fallback', p).forEach((it, i) => patternItem(it, `${p}.fallback[${i}]`));
  const plan = obj(m, 'external_plan', p);
  for (const [bucket, entries] of Object.entries(plan)) {
    if (!/^-?\d+$/.test(bucket)) throw new ProtocolError('MANIFEST_INVALID', `bucket inválido: ${bucket}`);
    if (!Array.isArray(entries)) throw new ProtocolError('MANIFEST_INVALID', `${p}.external_plan.${bucket} deve ser lista`);
    entries.forEach((e, i) => {
      const ep = `${p}.external_plan.${bucket}[${i}]`;
      if (!isObj(e)) throw new ProtocolError('MANIFEST_INVALID', `${ep} deve ser objeto`);
      str(e, 'i', ep);
      str(e, 'c', ep);
      int(e, 'u', ep);
      int(e, 'd', ep, { min: 1 });
    });
  }
  arr(m, 'emergency', p).forEach((e, i) => {
    const ep = `${p}.emergency[${i}]`;
    if (!isObj(e)) throw new ProtocolError('MANIFEST_INVALID', `${ep} deve ser objeto`);
    str(e, 'id', ep);
    str(e, 'c', ep);
    int(e, 'from_ms', ep);
    int(e, 'until_ms', ep);
  });
  arr(m, 'revoked', p).forEach((r, i) => {
    if (typeof r !== 'string') throw new ProtocolError('MANIFEST_INVALID', `${p}.revoked[${i}] deve ser string`);
  });
}

export function validateSignedManifest(v: unknown): asserts v is SignedManifest {
  if (!isObj(v)) throw new ProtocolError('MANIFEST_INVALID', 'resposta do manifesto deve ser objeto');
  if (v.alg !== 'ES256') throw new ProtocolError('MANIFEST_ALG', `alg não suportado: ${String(v.alg)}`);
  if (typeof v.key_id !== 'string' || typeof v.signature !== 'string') {
    throw new ProtocolError('MANIFEST_INVALID', 'key_id/signature ausentes');
  }
  validateManifest(v.manifest);
}

/** Lê com segurança um campo do envelope de erro `{"error":{code,message,details}}`. */
export function parseApiError(body: unknown): { code: string; message: string; details: unknown } | null {
  if (!isObj(body) || !isObj(body.error)) return null;
  const e = body.error;
  return {
    code: typeof e.code === 'string' ? e.code : 'UNKNOWN',
    message: typeof e.message === 'string' ? e.message : '',
    details: e.details ?? null,
  };
}
