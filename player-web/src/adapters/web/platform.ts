/**
 * Ponte de plataforma (BRIDGE.md, `rd-native/1`).
 *  - `window.RD_NATIVE` injetado pela casca (Windows/WebView2) ⇒ NativeBridge sobre
 *    `window.chrome.webview.postMessage` + evento "message" com envelopes {rd:1, kind, …}.
 *  - Ausente ⇒ WebBridge (navegador comum): telemetria limitada, sem reboot.
 * Comandos de NEGÓCIO (resync, clear_cache, redownload, reload_manifest) são do bundle; a ponte só cuida de
 * plataforma (restart_app, reboot, update_app, telemetria de sistema, sinal de vida do watchdog).
 */

import type { Orientation, Platform, PlayerState, TelemetryData } from '../../core/protocol';
import type { TechPinRecord } from '../../core/techPin';
import { uuidv4 } from '../../core/util';

export interface NativeConfig {
  api_base_url?: string;
  ws_base_url?: string;
  panel_url?: string;
  update_check_interval_hours?: number;
  config_ok?: boolean;
  config_errors?: string[];
}

export interface RdNative {
  bridge: string;
  platform: Platform | string;
  shell_version?: string;
  launched_by_watchdog?: boolean;
  config?: NativeConfig;
  capabilities?: string[];
}

export type BridgeCommandStatus = 'ACCEPTED' | 'DONE' | 'FAILED' | 'UNSUPPORTED';

export interface BridgeCommandResult {
  status: BridgeCommandStatus;
  detail?: string | null;
  delay_ms?: number;
}

export interface UpdateAppResult {
  /** rd-native/1 (rev. 2026-10-07): resposta rápida; o andamento chega pelo evento `update_status`. */
  status: 'NO_UPDATE' | 'AVAILABLE' | 'ACCEPTED' | 'REJECTED' | 'FAILED' | 'UNSUPPORTED';
  version?: string;
  detail?: string | null;
}

export interface ProgressInfo {
  state: PlayerState;
  creative_code?: string;
  item_started_at_ms?: number;
}

export type BridgeEventHandler = (event: string, data: Record<string, unknown>) => void;

export interface PlatformBridge {
  readonly native: boolean;
  readonly platform: Platform;
  readonly shellVersion: string | null;
  readonly capabilities: readonly string[];
  readonly config: NativeConfig | null;
  getTelemetry(): Promise<Partial<TelemetryData>>;
  command(cmd: { command_id: string; command: string; args: Record<string, unknown> | null }): Promise<BridgeCommandResult>;
  updateApp(params: { access_token?: string; mode?: 'check' | 'apply' | 'apply_now' }): Promise<UpdateAppResult>;
  reload(reason: string): Promise<void>;
  notifyReady(bundleVersion: string): void;
  notifyProgress(p: ProgressInfo): void;
  /** Repassa à casca o registro do PIN técnico (só sal+hash; nunca o PIN em claro) — BRIDGE_PROPOSTA `tech_pin`. */
  notifyTechPin(rec: TechPinRecord): void;
  log(level: 'debug' | 'info' | 'warn' | 'error', message: string): void;
  onEvent(fn: BridgeEventHandler): () => void;
}

// ───────────────────────── telemetria do navegador ─────────────────────────

export function screenResolution(): string | null {
  const s = globalThis.screen;
  if (!s) return null;
  const dpr = globalThis.devicePixelRatio || 1;
  return `${Math.round(s.width * dpr)}x${Math.round(s.height * dpr)}`;
}

export function screenOrientation(): Orientation | null {
  if (typeof globalThis.innerWidth !== 'number') return null;
  return globalThis.innerWidth >= globalThis.innerHeight ? 'LANDSCAPE' : 'PORTRAIT';
}

export function browserOs(): string | null {
  const nav = globalThis.navigator as unknown as { userAgentData?: { platform?: string }; userAgent?: string } | undefined;
  if (nav?.userAgentData?.platform) return nav.userAgentData.platform;
  const ua = nav?.userAgent ?? '';
  const m = /(Windows NT [\d.]+|Android [\d.]+|Mac OS X [\d_]+|CrOS|Linux|Tizen [\d.]+|Web0S)/.exec(ua);
  return m ? m[1]!.replace(/_/g, '.') : null;
}

async function browserTelemetry(): Promise<Partial<TelemetryData>> {
  const nav = globalThis.navigator as unknown as
    | {
        deviceMemory?: number;
        storage?: { estimate?: () => Promise<{ quota?: number; usage?: number }> };
      }
    | undefined;
  let disk_free_mb: number | null = null;
  let disk_total_mb: number | null = null;
  try {
    const e = await nav?.storage?.estimate?.();
    if (e && typeof e.quota === 'number' && typeof e.usage === 'number') {
      // Quota do navegador (não o disco físico) — melhor informação disponível na web.
      disk_total_mb = Math.floor(e.quota / 1048576);
      disk_free_mb = Math.floor((e.quota - e.usage) / 1048576);
    }
  } catch {
    /* ignore */
  }
  return {
    cpu_pct: null,
    ram_used_mb: null,
    ram_total_mb: typeof nav?.deviceMemory === 'number' ? Math.round(nav.deviceMemory * 1024) : null,
    disk_free_mb,
    disk_total_mb,
    uptime_s: Math.floor(globalThis.performance.now() / 1000),
    os: browserOs(),
    resolution: screenResolution(),
    orientation: screenOrientation(),
    wifi: null,
    hdmi_connected: null,
  };
}

// ───────────────────────── navegador comum ─────────────────────────

export class WebBridge implements PlatformBridge {
  readonly native = false;
  readonly platform: Platform = 'web';
  readonly shellVersion = null;
  readonly capabilities: readonly string[] = ['restart_app'];
  readonly config = null;

  getTelemetry(): Promise<Partial<TelemetryData>> {
    return browserTelemetry();
  }
  async command(cmd: { command: string }): Promise<BridgeCommandResult> {
    if (cmd.command === 'restart_app') return { status: 'ACCEPTED', delay_ms: 0, detail: 'recarregar a página' };
    return { status: 'UNSUPPORTED', detail: `não suportado no navegador: ${cmd.command}` };
  }
  async updateApp(): Promise<UpdateAppResult> {
    return { status: 'UNSUPPORTED', detail: 'navegador: atualização pelo Service Worker' };
  }
  async reload(): Promise<void> {
    globalThis.location.reload();
  }
  notifyReady(): void {}
  notifyProgress(): void {}
  notifyTechPin(): void {}
  log(): void {}
  onEvent(): () => void {
    return () => undefined;
  }
}

// ───────────────────────── casca nativa (rd-native/1) ─────────────────────────

export interface NativeTransport {
  postMessage(msg: unknown): void;
  addEventListener(type: 'message', fn: (ev: { data: unknown }) => void): void;
  removeEventListener?(type: 'message', fn: (ev: { data: unknown }) => void): void;
}

interface Envelope {
  rd: 1;
  kind: 'request' | 'response' | 'notify' | 'event';
  id?: string;
  method?: string;
  params?: Record<string, unknown>;
  ok?: boolean;
  result?: Record<string, unknown>;
  error?: { code: string; message: string };
  event?: string;
  data?: Record<string, unknown>;
}

export class BridgeError extends Error {
  constructor(
    public readonly code: string,
    message: string,
  ) {
    super(`${code}: ${message}`);
    this.name = 'BridgeError';
  }
}

export class NativeBridge implements PlatformBridge {
  readonly native = true;
  readonly platform: Platform;
  readonly shellVersion: string | null;
  readonly capabilities: readonly string[];
  readonly config: NativeConfig | null;
  private readonly pending = new Map<string, { resolve: (v: Record<string, unknown>) => void; reject: (e: unknown) => void; timer: unknown }>();
  private readonly handlers = new Set<BridgeEventHandler>();

  constructor(
    native: RdNative,
    private readonly transport: NativeTransport,
    private readonly timeoutMs = 10_000,
  ) {
    const p = String(native.platform ?? 'windows');
    this.platform = p === 'android' || p === 'web' ? p : 'windows';
    this.shellVersion = native.shell_version ?? null;
    this.capabilities = Array.isArray(native.capabilities) ? native.capabilities.slice() : [];
    this.config = native.config ?? null;
    transport.addEventListener('message', (ev) => this.onMessage(ev.data));
  }

  /** Pedido request → response com timeout. */
  request(method: string, params: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
    const id = uuidv4();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new BridgeError('TIMEOUT', `sem resposta da casca para ${method}`));
      }, this.timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      try {
        this.transport.postMessage({ rd: 1, kind: 'request', id, method, params } satisfies Envelope);
      } catch (e) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(e);
      }
    });
  }

  notify(method: string, params: Record<string, unknown> = {}): void {
    try {
      this.transport.postMessage({ rd: 1, kind: 'notify', method, params } satisfies Envelope);
    } catch {
      /* casca indisponível: ignora */
    }
  }

  async getTelemetry(): Promise<Partial<TelemetryData>> {
    try {
      const r = await this.request('get_telemetry');
      return { ...(await browserTelemetry()), ...(r as Partial<TelemetryData>) };
    } catch {
      return browserTelemetry();
    }
  }

  async command(cmd: { command_id: string; command: string; args: Record<string, unknown> | null }): Promise<BridgeCommandResult> {
    try {
      const r = await this.request('command', { command_id: cmd.command_id, command: cmd.command, args: cmd.args ?? {} });
      const status = String(r.status ?? 'FAILED') as BridgeCommandStatus;
      return {
        status: ['ACCEPTED', 'DONE', 'FAILED', 'UNSUPPORTED'].includes(status) ? status : 'FAILED',
        detail: typeof r.detail === 'string' ? r.detail : null,
        delay_ms: typeof r.delay_ms === 'number' ? r.delay_ms : undefined,
      };
    } catch (e) {
      if (e instanceof BridgeError && (e.code === 'UNSUPPORTED' || e.code === 'UNKNOWN_METHOD')) return { status: 'UNSUPPORTED', detail: e.message };
      return { status: 'FAILED', detail: e instanceof Error ? e.message : String(e) };
    }
  }

  async updateApp(params: { access_token?: string; mode?: 'check' | 'apply' | 'apply_now' }): Promise<UpdateAppResult> {
    try {
      const r = await this.request('update_app', params as Record<string, unknown>);
      return { status: (r.status as UpdateAppResult['status']) ?? 'FAILED', version: r.version as string | undefined, detail: (r.detail as string) ?? null };
    } catch (e) {
      return { status: 'FAILED', detail: e instanceof Error ? e.message : String(e) };
    }
  }

  async reload(reason: string): Promise<void> {
    await this.request('reload', { reason }).catch(() => globalThis.location?.reload());
  }

  notifyReady(bundleVersion: string): void {
    this.notify('ready', { bundle_version: bundleVersion });
  }

  notifyProgress(p: ProgressInfo): void {
    const params: Record<string, unknown> = { state: p.state };
    if (p.creative_code) params.creative_code = p.creative_code;
    if (typeof p.item_started_at_ms === 'number') params.item_started_at_ms = p.item_started_at_ms;
    this.notify('progress', params);
  }

  notifyTechPin(rec: TechPinRecord): void {
    // Lista fechada de campos: nada além do registro PBKDF2 sai pela ponte.
    this.notify('tech_pin', {
      algo: rec.algo,
      iterations: rec.iterations,
      salt_b64u: rec.salt_b64u,
      hash_b64u: rec.hash_b64u,
      set_at_ms: rec.set_at_ms,
    });
  }

  log(level: 'debug' | 'info' | 'warn' | 'error', message: string): void {
    this.notify('log', { level, message: message.slice(0, 2_000) });
  }

  onEvent(fn: BridgeEventHandler): () => void {
    this.handlers.add(fn);
    return () => this.handlers.delete(fn);
  }

  private onMessage(raw: unknown): void {
    let msg: unknown = raw;
    if (typeof raw === 'string') {
      try {
        msg = JSON.parse(raw);
      } catch {
        return;
      }
    }
    const m = msg as Envelope;
    if (!m || typeof m !== 'object' || m.rd !== 1) return;
    if (m.kind === 'response' && typeof m.id === 'string') {
      const p = this.pending.get(m.id);
      if (!p) return;
      this.pending.delete(m.id);
      clearTimeout(p.timer as ReturnType<typeof setTimeout>);
      if (m.ok) p.resolve(m.result ?? {});
      else p.reject(new BridgeError(m.error?.code ?? 'INTERNAL', m.error?.message ?? 'erro da casca'));
    } else if (m.kind === 'event' && typeof m.event === 'string') {
      for (const h of this.handlers) h(m.event, m.data ?? {});
    }
  }
}

/** Escolhe a ponte: `window.RD_NATIVE` + `chrome.webview` ⇒ nativa; senão navegador. */
export function createBridge(g: typeof globalThis = globalThis): PlatformBridge {
  const native = (g as unknown as { RD_NATIVE?: RdNative }).RD_NATIVE;
  const webview = (g as unknown as { chrome?: { webview?: NativeTransport } }).chrome?.webview;
  if (native && native.bridge === 'rd-native/1' && webview) return new NativeBridge(native, webview);
  return new WebBridge();
}
