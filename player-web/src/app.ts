/**
 * Orquestração do player web (navegador e casca Windows/WebView2): pareamento → autenticação → WS →
 * manifesto/sincronização → reprodução → PoP/outbox → comandos/kill → alertas → revogação.
 * Toda a lógica de protocolo está no núcleo (`src/core`); aqui só há a "fiação" com DOM/IndexedDB/OPFS/ponte.
 */

import type { PlatformBridge } from './adapters/web/platform';
import type { PlayerConfig } from './config';
import { computeAlerts, type AlertState } from './core/alerts';
import { ApiClient, ApiError } from './core/api';
import { DeviceAuth } from './core/auth';
import { Clock, type ClockRecord } from './core/clock';
import { CommandProcessor, type HandlerOutcome } from './core/commands';
import { createResolveContext } from './core/cycle';
import { DownloadError, DownloadQueue, downloadFile, type DownloadJob, type JobExecutor } from './core/downloader';
import { KillList } from './core/kills';
import { TechPinStore } from './core/techPin';
import { PairingFlow } from './core/pairing';
import { PlaybackEngine, type PlaybackContext, type RenderItem } from './core/playback';
import { PopRecorder } from './core/pop';
import {
  PROTOCOL_PATHS,
  type BatchResponse,
  type CommandData,
  type DeviceInfo,
  type HeartbeatData,
  type Manifest,
  type Orientation,
  type PairingRequestResponse,
  type PairingResult,
  type PlayerState,
  type ServerKey,
  type SignedPopEvent,
  type TelemetryData,
  type WsTicketResponse,
} from './core/protocol';
import { STATE_KEYS, type KeyStore, type MediaStore, type StateStore } from './core/storage';
import { SyncEngine, type StoredManifest, type SyncEvent } from './core/sync';
import { backoffDelay, errorMessage, realMono, sleep } from './core/util';
import { PlayerChannel, type WebSocketFactory } from './core/ws';
import type { FooterView } from './ui/footer';
import type { Overlays, TechClaim } from './ui/overlays';
import type { DomRenderer } from './ui/renderer';
import { WebWatchdog } from './ui/watchdog';

export interface DeviceRecord extends PairingResult {
  paired_at_ms: number;
}

interface AnchorRecord {
  screen_id: string;
  anchor_ms: number;
  confirmed: boolean;
}

interface SyncInfo {
  last_sync_ms: number | null;
  last_contact_ms: number | null;
  last_play_ms: number | null;
}

export interface AppEnv {
  config: PlayerConfig;
  bridge: PlatformBridge;
  state: StateStore;
  keys: KeyStore;
  media: MediaStore;
  /** Executor de download no Worker (null ⇒ thread principal). */
  workerExecutor: ((urlFor: (j: DownloadJob) => string, headers: () => Promise<Record<string, string>>) => JobExecutor) | null;
  renderer: DomRenderer;
  footer: FooterView;
  overlays: Overlays;
  /** Rotação por software da tela lógica (settings.orientation × orientação física). */
  orienter: { setConfigured(o: Orientation | null): boolean; relayout(): boolean };
  wsFactory: WebSocketFactory;
  storagePersistent: boolean;
  estimate: () => Promise<{ quota: number; usage: number } | null>;
  log: (level: 'info' | 'warn' | 'error', msg: string) => void;
  reloadPage: () => void;
  techMode: boolean;
}

export class PlayerApp {
  private readonly clock = new Clock();
  private readonly kills: KillList;
  private device: DeviceRecord | null = null;
  private anchor: AnchorRecord | null = null;
  private info: SyncInfo = { last_sync_ms: null, last_contact_ms: null, last_play_ms: null };
  private readonly api: ApiClient;
  private readonly auth: DeviceAuth;
  private readonly pop: PopRecorder;
  private readonly sync: SyncEngine;
  private readonly engine: PlaybackEngine;
  private channel: PlayerChannel | null = null;
  private readonly commands: CommandProcessor;
  private readonly techPin: TechPinStore;
  private watchdog: WebWatchdog | null = null;
  private revoked = false;
  private paired = false;
  private frozen = false;
  private frozenSince = 0;
  private diskFull = false;
  private ctxCache: { key: string; ctx: PlaybackContext } | null = null;
  private stagedCache: { key: string; ctx: PlaybackContext } | null = null;
  private signer: Awaited<ReturnType<KeyStore['load']>> = null;
  private manifestInflight: Promise<void> | null = null;
  private manifestAgain = false;
  private flushTimer: ReturnType<typeof setTimeout> | null = null;
  private flushFailures = 0;
  private lastManifestCheckMono = Number.NEGATIVE_INFINITY;
  private pairingCtrl: AbortController | null = null;
  private currentPairing: { flow: PairingFlow; req: PairingRequestResponse } | null = null;
  private alerts: AlertState | null = null;
  private storageStat: { free: number | null; used: number | null } = { free: null, used: null };

  constructor(private readonly env: AppEnv) {
    this.kills = new KillList(env.state, () => this.clock.now());
    this.techPin = new TechPinStore(env.state);
    this.api = new ApiClient({ baseUrl: env.config.apiBase, clock: this.clock, onRevoked: () => void this.handleRevoked() });
    const unauth = new ApiClient({ baseUrl: env.config.apiBase, clock: this.clock });
    this.auth = new DeviceAuth({
      api: unauth,
      signer: () => this.signer,
      deviceId: () => this.device?.device_id ?? null,
      onRevoked: () => void this.handleRevoked(),
      onServerTime: (ms) => this.clock.observe(ms),
    });
    this.api.auth = this.auth;
    this.pop = new PopRecorder({
      store: env.state,
      signer: () => this.signer,
      identity: () => (this.device ? { device_id: this.device.device_id, screen_id: this.device.screen_id } : null),
      onRecorded: (e) => this.onPopRecorded(e),
    });
    const mediaUrl = (j: DownloadJob): string => this.api.url(PROTOCOL_PATHS.media(j.media_id));
    const authHeaders = async (): Promise<Record<string, string>> => ({ Authorization: `Bearer ${await this.auth.token()}` });
    const base: JobExecutor =
      env.workerExecutor?.(mediaUrl, authHeaders) ??
      (async (job, ctx) => {
        await downloadFile(job, {
          fetch: (u, i) => fetch(u, i),
          url: mediaUrl(job),
          headers: await authHeaders(),
          store: env.media,
          signal: ctx.signal,
          onProgress: ctx.onProgress,
        });
      });
    const executor: JobExecutor = async (job, ctx) => {
      try {
        await base(job, ctx);
      } catch (e) {
        if (e instanceof DownloadError && e.kind === 'AUTH') this.auth.invalidate();
        throw e;
      }
    };
    const queue = new DownloadQueue({
      executor,
      concurrency: 2,
      onProgress: (p) => {
        this.sync.progress = p;
      },
    });
    this.sync = new SyncEngine({
      store: env.state,
      media: env.media,
      queue,
      serverKeys: () => this.device?.server_keys ?? [],
      screenId: () => this.device?.screen_id ?? null,
      estimate: env.estimate,
      now: () => this.clock.now(),
      emit: (e) => this.onSyncEvent(e),
      log: (m) => env.log('warn', m),
    });
    this.engine = new PlaybackEngine({
      renderer: env.renderer,
      clock: this.clock,
      mono: realMono,
      getContext: () => this.context(),
      peekStagedContext: () => this.stagedContext(),
      hasStaged: () => this.sync.hasStaged(),
      commitStaged: async () => (await this.sync.commit()) !== null,
      mediaFor: (ctx, key) => this.mediaFor(ctx, key),
      pop: this.pop,
      // "offline" = sem WS e sem contato HTTP com o servidor no último minuto.
      isOffline: () => !(this.channel?.connected ?? false) && this.clock.now() - (this.info.last_contact_ms ?? 0) > 60_000,
      onItemStart: (i) => {
        env.bridge.notifyProgress({ state: this.bridgeState(), creative_code: i.code ?? undefined, item_started_at_ms: i.startedAtMs });
      },
      onPopResult: (r) => {
        if (r === 'COMPLETED') {
          this.info.last_play_ms = this.clock.now();
          void this.saveInfo();
        }
      },
      onMediaError: (key, reason) => this.reportMediaError(key, reason),
      log: (m) => env.log('warn', m),
    });
    this.commands = new CommandProcessor({
      store: env.state,
      now: () => this.clock.now(),
      send: (type, data) => this.channel?.send(type, data) ?? false,
      handlers: this.commandHandlers(),
    });
  }

  // ───────────────────────── inicialização ─────────────────────────

  async start(): Promise<void> {
    const s = this.env.state;
    this.clock.load(await s.get<ClockRecord>(STATE_KEYS.clock));
    await this.kills.load();
    this.kills.onChange(() => {
      this.ctxCache = null;
      this.stagedCache = null;
      this.engine.refresh();
    });
    this.info = (await s.get<SyncInfo>(STATE_KEYS.syncInfo)) ?? this.info;
    this.device = (await s.get<DeviceRecord>(STATE_KEYS.device)) ?? null;
    this.anchor = (await s.get<AnchorRecord>(STATE_KEYS.anchor)) ?? null;
    this.signer = await this.env.keys.load().catch(() => null);
    await this.sync.load();
    this.applySettings(this.sync.activeManifest()?.signed.manifest ?? null);

    // Pareado: o contexto já existe no 1º plano do motor (boot no meio do item ⇒ FILLER + SKIPPED na hora,
    // sem 5 s de tela institucional). Achado do E2E real.
    this.paired = this.device !== null && this.signer !== null;
    // Reprodução começa do cache local imediatamente (offline após queda de energia — ETAPA2 #277).
    this.engine.start();
    this.startWatchdog();
    this.startTimers();
    this.env.bridge.notifyReady(this.env.config.appVersion);
    // Após `ready`, reenvia à casca o PIN técnico já gravado (a casca ignora registro igual ou mais antigo).
    void this.techPin
      .get()
      .then((rec) => {
        if (rec) this.env.bridge.notifyTechPin(rec);
      })
      .catch(() => undefined);
    this.env.bridge.onEvent((ev, data) => {
      if (ev === 'power_resumed') this.channel?.socket.kick();
      if (ev === 'update_status') {
        const st = String(data.state ?? '');
        this.env.log(st === 'FAILED' || st === 'ROLLED_BACK' ? 'warn' : 'info', `atualização: ${st} ${String(data.version ?? '')} ${String(data.detail ?? '')}`);
        if (st === 'FAILED' || st === 'ROLLED_BACK') void this.commands.complete('update_app', 'FAILED', `${st}: ${String(data.detail ?? '')}`);
        if (st === 'APPLIED') void this.commands.complete('update_app', 'DONE', `APPLIED ${String(data.version ?? '')}`);
      }
      if (ev === 'watchdog_reload') this.env.log('warn', `recarregado pelo watchdog da casca: ${String(data.reason ?? '')}`);
      if (ev === 'display_changed') {
        this.env.orienter.relayout();
        this.env.footer.relayout();
      }
    });

    if (this.paired) {
      this.goOnline();
    } else {
      void this.pairingLoop();
    }
  }

  private startTimers(): void {
    // Alertas/rodapé e persistência do relógio (timers locais — não é polling de rede).
    const tick = (): void => {
      this.refreshAlerts();
      void this.env.state.set(STATE_KEYS.clock, this.clock.snapshot());
    };
    tick();
    setInterval(tick, 30_000);
    // Sinal de vida para o watchdog da casca (≤ 10 s).
    setInterval(() => {
      const cur = this.engine.current();
      this.env.bridge.notifyProgress({
        state: this.bridgeState(),
        creative_code: cur?.code ?? undefined,
        item_started_at_ms: cur?.startedAtMs,
      });
    }, 5_000);
    // Estatística de armazenamento para o heartbeat.
    const stat = async (): Promise<void> => {
      const e = await this.env.estimate();
      this.storageStat = e ? { free: Math.max(0, e.quota - e.usage), used: e.usage } : { free: null, used: null };
    };
    void stat();
    setInterval(() => void stat(), 5 * 60_000);
    globalThis.addEventListener('online', () => this.channel?.socket.kick());
    globalThis.addEventListener('resize', () => {
      this.env.orienter.relayout();
      this.env.footer.relayout();
    });
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'hidden') this.engine.markDegraded('PAGE_HIDDEN');
    });
  }

  private startWatchdog(): void {
    this.watchdog = new WebWatchdog({
      renderer: this.env.renderer,
      onRecover: (reason) => {
        this.env.log('warn', `watchdog: ${reason}`);
        this.engine.recover(reason);
      },
      onReload: (reason) => {
        this.env.log('error', `watchdog: recarregando (${reason})`);
        if (this.env.bridge.native) void this.env.bridge.reload(`WATCHDOG ${reason}`);
        else this.env.reloadPage();
      },
      onFrozen: (f) => {
        if (f && !this.frozen) this.frozenSince = realMono();
        this.frozen = f;
      },
    });
    this.watchdog.start();
  }

  // ───────────────────────── contexto de reprodução ─────────────────────────

  private anchorFor(m: Manifest): number | null {
    if (m.anchor_ms !== null) return m.anchor_ms;
    if (this.anchor && this.anchor.screen_id === m.screen_id) return this.anchor.anchor_ms;
    return null;
  }

  private buildContext(sm: StoredManifest, cacheKeyPrefix: string): { key: string; ctx: PlaybackContext } | null {
    if (this.revoked || !this.paired) return null;
    const m = sm.signed.manifest;
    const A = this.anchorFor(m);
    if (A === null) return null;
    const ready = this.sync.mediaReadyKeys(m);
    const killIds = this.kills.ids();
    const key = `${cacheKeyPrefix}:${sm.version}:${A}:${ready.length}:${killIds.length}`;
    return { key, ctx: { manifest: m, version: sm.version, resolve: createResolveContext(m, A, { revoked: killIds, media_ready: ready }) } };
  }

  private context(): PlaybackContext | null {
    const sm = this.sync.activeManifest();
    if (!sm) return null;
    const probe = this.buildKey(sm, 'a');
    if (this.ctxCache && this.ctxCache.key === probe) return this.ctxCache.ctx;
    const built = this.buildContext(sm, 'a');
    this.ctxCache = built;
    return built?.ctx ?? null;
  }

  private stagedContext(): PlaybackContext | null {
    const sm = this.sync.stagedManifest();
    if (!sm) return null;
    const probe = this.buildKey(sm, 's');
    if (this.stagedCache && this.stagedCache.key === probe) return this.stagedCache.ctx;
    const built = this.buildContext(sm, 's');
    this.stagedCache = built;
    return built?.ctx ?? null;
  }

  private buildKey(sm: StoredManifest, prefix: string): string {
    const m = sm.signed.manifest;
    return `${prefix}:${sm.version}:${this.anchorFor(m)}:${this.sync.mediaReadyKeys(m).length}:${this.kills.ids().length}`;
  }

  private async mediaFor(ctx: PlaybackContext, key: string): Promise<RenderItem | null> {
    const c = ctx.manifest.creatives[key];
    const me = c ? ctx.manifest.media[c.media] : undefined;
    if (!c || !me || !this.sync.isPresent(me.sha256)) return null;
    const url = await this.env.media.getUrl(me.sha256);
    let released = false;
    return {
      creativeKey: key,
      mediaKey: c.media,
      sha256: me.sha256,
      mime: me.mime,
      isVideo: me.mime.startsWith('video/'),
      mediaDurationMs: me.duration_ms,
      url,
      label: c.label,
      code: c.code,
      release: () => {
        if (released) return;
        released = true;
        this.env.media.releaseUrl(url);
      },
    };
  }

  /** Chamado no boot e quando um manifesto é ATIVADO (troca só na fronteira de ciclo). */
  private applySettings(m: Manifest | null): void {
    if (this.env.orienter.setConfigured(m?.settings.orientation ?? null)) {
      this.env.log('info', `orientação configurada: ${m?.settings.orientation ?? '—'} (rotação por software aplicada)`);
    }
    const f = m?.settings.footer ?? { enabled: true, height_permille: 60 };
    this.env.footer.setLayout({ enabled: f.enabled, heightPermille: f.height_permille });
  }

  // ───────────────────────── âncora (RDPP §5.1.1) ─────────────────────────

  private async ensureAnchor(m: Manifest): Promise<void> {
    if (m.anchor_ms !== null) {
      if (!this.anchor || this.anchor.anchor_ms !== m.anchor_ms || this.anchor.screen_id !== m.screen_id || !this.anchor.confirmed) {
        await this.saveAnchor({ screen_id: m.screen_id, anchor_ms: m.anchor_ms, confirmed: true });
      }
      return;
    }
    if (this.anchor && this.anchor.screen_id === m.screen_id) {
      if (!this.anchor.confirmed) void this.postAnchor();
      return;
    }
    // Primeiro start válido: manifesto ativo e mídia pronta ⇒ âncora = agora (arredondado ao segundo).
    const a = Math.floor(this.clock.now() / 1000) * 1000;
    await this.saveAnchor({ screen_id: m.screen_id, anchor_ms: a, confirmed: false });
    void this.postAnchor();
  }

  private async saveAnchor(rec: AnchorRecord): Promise<void> {
    await this.env.state.set(STATE_KEYS.anchor, rec);
    this.anchor = rec;
    this.ctxCache = null;
    this.stagedCache = null;
    this.engine.refresh();
  }

  private async postAnchor(): Promise<void> {
    const a = this.anchor;
    if (!a || a.confirmed || !this.paired) return;
    try {
      const res = await this.api.post<{ anchor_ms: number }>(PROTOCOL_PATHS.anchor, { anchor_ms: a.anchor_ms });
      const server = Number(res.data?.anchor_ms);
      if (Number.isFinite(server)) {
        // Write-once no servidor: se outro dispositivo da tela venceu, adota o valor existente.
        await this.saveAnchor({ screen_id: a.screen_id, anchor_ms: server, confirmed: true });
      }
    } catch (e) {
      this.env.log('warn', `âncora: envio pendente (${errorMessage(e)})`);
    }
  }

  // ───────────────────────── online: WS, manifesto, outbox ─────────────────────────

  private goOnline(): void {
    if (this.channel) return;
    this.channel = new PlayerChannel({
      url: async () => {
        const res = await this.api.post<WsTicketResponse>(PROTOCOL_PATHS.wsTicket, {});
        if (!res.data?.ticket) throw new Error('ticket ausente');
        // HTTP ok mas WS pode estar fora: garante sincronização ocasional (retry, não polling).
        if (realMono() - this.lastManifestCheckMono > 10 * 60_000) void this.fetchManifest();
        return `${this.env.config.wsBase}${PROTOCOL_PATHS.wsPlayer}?ticket=${encodeURIComponent(res.data.ticket)}`;
      },
      factory: this.env.wsFactory,
      settings: () => {
        const st = this.sync.activeManifest()?.signed.manifest.settings;
        return { heartbeat_interval_ms: st?.heartbeat_interval_ms ?? 30_000, telemetry_interval_ms: st?.telemetry_interval_ms ?? 300_000 };
      },
      now: () => this.clock.now(),
      log: (m) => this.env.log('warn', m),
      handlers: {
        heartbeat: () => this.heartbeat(),
        telemetry: () => this.telemetry(),
        onContact: () => this.markContact(),
        onServerTime: (ms) => this.clock.observe(ms),
        onSyncRequired: (v) => {
          const known = this.sync.knownVersion();
          if (v === null || known === null || v > known) void this.fetchManifest();
        },
        onCommand: (cmd) => void this.commands.handle(cmd),
        onKill: (k) => void this.applyKill(k.creative_ids),
        onRevoked: () => void this.handleRevoked(),
        onFlush: () => void this.flushOutbox(),
        onConnected: () => {
          this.env.log('info', 'WS conectado');
          void this.fetchManifest();
          void this.flushOutbox();
          void this.commands.resumePending();
          void this.postAnchor();
          void this.sync.repair();
        },
        onDisconnected: (info) => this.env.log('warn', `WS desconectado (${info.code}); nova tentativa em ${info.retryInMs ?? '-'} ms`),
      },
    });
    this.channel.start();
    // HTTP não depende do WS: sincroniza e envia o outbox já (o WS pode estar fora).
    void this.fetchManifest();
    void this.flushOutbox();
    void this.postAnchor();
  }

  private markContact(): void {
    const now = this.clock.now();
    const prev = this.info.last_contact_ms ?? 0;
    this.info.last_contact_ms = now;
    if (now - prev > 60_000) {
      void this.saveInfo();
      this.refreshAlerts();
    }
  }

  private markSynced(): void {
    this.info.last_sync_ms = this.clock.now();
    this.info.last_contact_ms = this.info.last_sync_ms;
    void this.saveInfo();
    this.refreshAlerts();
  }

  private async saveInfo(): Promise<void> {
    await this.env.state.set(STATE_KEYS.syncInfo, this.info).catch(() => undefined);
  }

  /** GET /manifest com If-None-Match; single-flight com nova rodada se pedido durante a anterior. */
  fetchManifest(force = false): Promise<void> {
    if (this.manifestInflight) {
      this.manifestAgain = true;
      return this.manifestInflight;
    }
    this.manifestInflight = (async () => {
      do {
        this.manifestAgain = false;
        if (!this.paired || this.revoked) return;
        this.lastManifestCheckMono = realMono();
        const known = force ? null : this.sync.knownVersion();
        try {
          const res = await this.api.get<unknown>(PROTOCOL_PATHS.manifest, {
            headers: known !== null ? { 'If-None-Match': `"${known}"` } : {},
            accept: [304],
          });
          this.markSynced();
          if (res.status === 200 && res.data) void this.sync.receive(res.data);
        } catch (e) {
          if (e instanceof ApiError && e.isRevoked) return;
          this.env.log('warn', `manifesto: ${errorMessage(e)}`);
        }
        force = false;
      } while (this.manifestAgain);
    })().finally(() => {
      this.manifestInflight = null;
    });
    return this.manifestInflight;
  }

  private onPopRecorded(_e: SignedPopEvent): void {
    // Envio em lote com atraso (agrupa eventos); o outbox persiste offline indefinidamente.
    if (this.flushTimer || !this.paired || this.revoked) return;
    this.flushTimer = setTimeout(() => {
      this.flushTimer = null;
      void this.flushOutbox();
    }, 15_000);
  }

  private flushing: Promise<void> | null = null;

  flushOutbox(): Promise<void> {
    if (this.flushing) return this.flushing;
    this.flushing = (async () => {
      if (!this.paired || this.revoked) return;
      try {
        await this.pop.flush(async (events) => {
          const res = await this.api.post<BatchResponse>(PROTOCOL_PATHS.eventsBatch, { events });
          return res.data ?? { results: [], ack_upto_seq: null };
        });
        this.flushFailures = 0;
        this.markContact();
      } catch (e) {
        this.flushFailures++;
        const delay = backoffDelay(this.flushFailures, 5_000, 5 * 60_000);
        this.env.log('warn', `outbox: falha no envio (${errorMessage(e)}); nova tentativa em ${delay} ms`);
        if (!this.flushTimer) {
          this.flushTimer = setTimeout(() => {
            this.flushTimer = null;
            void this.flushOutbox();
          }, delay);
        }
      }
    })().finally(() => {
      this.flushing = null;
    });
    return this.flushing;
  }

  private async applyKill(ids: string[]): Promise<void> {
    const added = await this.kills.add(ids);
    if (added.length) this.env.log('warn', `kill aplicado: ${added.join(', ')}`);
  }

  // ───────────────────────── eventos de sincronização ─────────────────────────

  private onSyncEvent(e: SyncEvent): void {
    switch (e.type) {
      case 'manifest_applied': {
        this.channel?.send('manifest_applied', { version: e.version });
        const m = this.sync.activeManifest()?.signed.manifest;
        if (m) {
          this.applySettings(m);
          void this.ensureAnchor(m);
          // Kill suspenso no painel: o manifesto gerado depois do kill é a fonte de verdade.
          void this.kills.reconcile(m.revoked, m.generated_at_ms).then((lifted) => {
            if (lifted.length) this.env.log('info', `kill suspenso pelo servidor: ${lifted.join(', ')}`);
          });
        }
        this.ctxCache = null;
        this.stagedCache = null;
        this.diskFull = false;
        this.engine.refresh();
        this.refreshAlerts();
        break;
      }
      case 'manifest_failed':
        this.channel?.send('manifest_failed', { version: e.version, reason: e.reason });
        break;
      case 'media_error':
        this.channel?.send('media_error', { media_id: e.media_id, reason: e.reason, attempts: e.attempts });
        break;
      case 'media_ready':
        this.ctxCache = null;
        this.engine.refresh();
        break;
      case 'disk_full':
        this.diskFull = true;
        this.refreshAlerts();
        break;
      case 'staged':
        this.stagedCache = null;
        break;
      default:
        break;
    }
  }

  private reportMediaError(creativeKey: string, reason: string): void {
    const m = this.sync.activeManifest()?.signed.manifest;
    const c = m?.creatives[creativeKey];
    const media = c ? m?.media[c.media] : undefined;
    if (media) this.channel?.send('media_error', { media_id: media.media_id, reason, attempts: 0 });
  }

  // ───────────────────────── heartbeat / telemetria ─────────────────────────

  private playerState(): PlayerState {
    if (!this.paired || this.revoked) return 'UNPAIRED';
    if (this.frozen) return 'FROZEN';
    const st = this.sync.state;
    if (st === 'DOWNLOADING' || st === 'PLAN' || st === 'VERIFY_SHA' || st === 'VERIFY_SIG' || st === 'RECEIVED') return 'SYNCING';
    if (this.engine.current()?.step.seg && this.context()) return 'PLAYING';
    if (st === 'FAILED' && !this.sync.activeManifest()) return 'ERROR';
    return 'IDLE';
  }

  /**
   * Estado para o watchdog da casca (`progress`): FROZEN só se a recuperação interna não resolveu em 15 s
   * (FROZEN faz a casca recarregar o WebView — não duplicar a reação a um travamento já recuperado).
   */
  private bridgeState(): PlayerState {
    const s = this.playerState();
    if (s === 'FROZEN' && realMono() - this.frozenSince < 15_000) return 'PLAYING';
    return s;
  }

  private heartbeat(): HeartbeatData {
    const cur = this.engine.current();
    return {
      state: this.playerState(),
      current: cur?.code ? { creative_code: cur.code, started_at_ms: cur.startedAtMs } : null,
      manifest_version_applied: this.sync.activeManifest()?.version ?? null,
      manifest_version_pending: this.sync.pendingVersion,
      download: this.sync.progress ? { done_bytes: this.sync.progress.done_bytes, total_bytes: this.sync.progress.total_bytes } : null,
      last_sync_ms: this.info.last_sync_ms,
      last_play_ms: this.info.last_play_ms,
      storage: { free_bytes: this.storageStat.free, used_bytes: this.storageStat.used, persistent: this.env.storagePersistent },
      clock_offset_ms: this.clock.offsetMs,
      alerts: this.alerts?.codes ?? [],
    };
  }

  private async telemetry(): Promise<TelemetryData> {
    const t = await this.env.bridge.getTelemetry();
    return {
      cpu_pct: t.cpu_pct ?? null,
      ram_used_mb: t.ram_used_mb ?? null,
      ram_total_mb: t.ram_total_mb ?? null,
      disk_free_mb: t.disk_free_mb ?? null,
      disk_total_mb: t.disk_total_mb ?? null,
      uptime_s: t.uptime_s ?? null,
      os: t.os ?? null,
      app_version: this.env.config.appVersion,
      platform: this.env.config.platform,
      resolution: t.resolution ?? null,
      orientation: t.orientation ?? null,
      wifi: t.wifi ?? null,
      hdmi_connected: t.hdmi_connected ?? null,
    };
  }

  private refreshAlerts(): void {
    const m = this.sync.activeManifest()?.signed.manifest ?? null;
    const lastSync = Math.max(this.info.last_sync_ms ?? 0, this.info.last_contact_ms ?? 0) || null;
    this.alerts = computeAlerts({
      now: this.clock.now(),
      lastSyncMs: this.paired ? lastSync : null,
      offlineAlertDays: m?.settings.offline_alert_days,
      clock: this.clock.status(m?.generated_at_ms ?? null),
      storagePersistent: this.env.storagePersistent,
      diskFull: this.diskFull,
    });
    this.env.footer.setAlert(this.alerts.footer);
    this.env.overlays.showAlert28(this.paired && !this.revoked ? this.alerts.overlay : null);
  }

  // ───────────────────────── comandos ─────────────────────────

  private commandHandlers(): Record<string, (cmd: CommandData) => Promise<HandlerOutcome>> {
    const platform = async (cmd: CommandData): Promise<HandlerOutcome> => {
      const r = await this.env.bridge.command({ command_id: cmd.command_id, command: cmd.command, args: cmd.args });
      if (r.status === 'ACCEPTED') {
        return {
          status: 'RESTARTING',
          detail: r.detail ?? null,
          restart: () => {
            if (!this.env.bridge.native && cmd.command === 'restart_app') setTimeout(() => this.env.reloadPage(), r.delay_ms ?? 0);
          },
        };
      }
      return { status: r.status === 'DONE' ? 'DONE' : r.status === 'UNSUPPORTED' ? 'UNSUPPORTED' : 'FAILED', detail: r.detail ?? null };
    };
    return {
      restart_app: platform,
      reboot: platform,
      resync: async () => {
        await this.fetchManifest(true);
        void this.sync.repair();
        void this.flushOutbox();
        return { status: 'DONE' };
      },
      set_tech_pin: async (cmd) => {
        // Só sal+hash PBKDF2 (o servidor nunca envia o PIN em claro); guardado no IndexedDB.
        const rec = await this.techPin.set(cmd.args, this.clock.now());
        // Casca nativa: mesma fonte de verdade para o menu técnico nativo (BRIDGE_PROPOSTA `tech_pin`).
        this.env.bridge.notifyTechPin(rec);
        return { status: 'DONE', detail: 'PIN técnico atualizado' };
      },
      reload_manifest: async () => {
        await this.fetchManifest(true);
        return { status: 'DONE' };
      },
      clear_cache: async () => {
        await this.sync.reset({ media: true });
        await this.clearShellCache();
        this.ctxCache = null;
        this.engine.refresh();
        await this.fetchManifest(true);
        return { status: 'DONE', detail: 'cache limpo; manifesto rebaixado' };
      },
      redownload: async () => {
        await this.sync.reset({ media: true });
        this.ctxCache = null;
        this.engine.refresh();
        await this.fetchManifest(true);
        return { status: 'DONE', detail: 'mídias removidas; download refeito' };
      },
      update_app: async () => {
        if (this.env.bridge.native) {
          // rd-native/1: ACCEPTED = download em segundo plano; a casca reinicia na versão nova (DONE ao voltar)
          // ou avisa falha pelo evento update_status (FAILED/ROLLED_BACK ⇒ command_ack FAILED).
          const r = await this.env.bridge.updateApp({ access_token: await this.auth.token(), mode: 'apply_now' });
          if (r.status === 'ACCEPTED') return { status: 'RESTARTING', detail: r.version ?? null, restart: () => undefined };
          if (r.status === 'NO_UPDATE') return { status: 'DONE', detail: 'NO_UPDATE' };
          return { status: r.status === 'UNSUPPORTED' ? 'UNSUPPORTED' : 'FAILED', detail: r.detail ?? r.status };
        }
        const reg = await navigator.serviceWorker?.getRegistration?.().catch(() => undefined);
        if (!reg) return { status: 'UNSUPPORTED', detail: 'sem Service Worker' };
        await reg.update();
        return { status: 'RESTARTING', detail: 'app shell atualizado', restart: () => setTimeout(() => this.env.reloadPage(), 500) };
      },
    };
  }

  private async clearShellCache(): Promise<void> {
    try {
      const keys = await caches.keys();
      await Promise.all(keys.filter((k) => k.startsWith('rd-player-shell')).map((k) => caches.delete(k)));
    } catch {
      /* sem Cache Storage */
    }
  }

  // ───────────────────────── revogação e pareamento ─────────────────────────

  /** DEVICE_REVOKED: para de reproduzir conteúdo (pertence à RD/anunciantes), limpa credenciais e volta a parear. */
  private async handleRevoked(): Promise<void> {
    if (this.revoked) return;
    this.revoked = true;
    this.paired = false;
    this.env.log('warn', 'dispositivo revogado: parando reprodução e voltando ao pareamento');
    this.channel?.stop();
    this.channel = null;
    this.auth.invalidate();
    this.ctxCache = null;
    this.stagedCache = null;
    this.engine.refresh();
    this.env.renderer.clear();
    this.env.overlays.showAlert28(null);
    this.env.overlays.showMessage('Dispositivo desvinculado', 'Esta tela foi revogada no painel. Iniciando novo pareamento…');
    try {
      await this.sync.reset({ media: true });
      await this.env.state.outboxClear();
      await this.env.state.transact({ del: [STATE_KEYS.device, STATE_KEYS.anchor, STATE_KEYS.commands, STATE_KEYS.syncInfo] });
      await this.kills.clear();
      await this.env.keys.clear();
    } catch (e) {
      this.env.log('error', `limpeza na revogação: ${errorMessage(e)}`);
    }
    this.device = null;
    this.anchor = null;
    this.signer = null;
    this.info = { last_sync_ms: null, last_contact_ms: null, last_play_ms: null };
    await sleep(3_000);
    void this.pairingLoop();
  }

  private deviceInfo(): DeviceInfo {
    const res = `${Math.round(screen.width * (devicePixelRatio || 1))}x${Math.round(screen.height * (devicePixelRatio || 1))}`;
    const ua = navigator.userAgent;
    return {
      model: this.env.bridge.native ? `RD Player ${this.env.config.platform}` : 'Navegador',
      os: /Windows NT [\d.]+|Android [\d.]+|Mac OS X [\d_.]+|CrOS|Linux|Tizen|Web0S/.exec(ua)?.[0] ?? 'desconhecido',
      resolution: res,
    };
  }

  private async pairingLoop(): Promise<void> {
    this.pairingCtrl?.abort();
    const ctrl = new AbortController();
    this.pairingCtrl = ctrl;
    const ov = this.env.overlays;
    ov.hideMessage();
    try {
      this.signer = (await this.env.keys.load()) ?? (await this.env.keys.create());
    } catch (e) {
      ov.showMessage('Erro de segurança', `Não foi possível criar a chave do dispositivo: ${errorMessage(e)}`);
      return;
    }
    const unauth = new ApiClient({ baseUrl: this.env.config.apiBase, clock: this.clock });
    const flow = new PairingFlow({
      api: unauth,
      signer: this.signer,
      platform: this.env.config.platform,
      appVersion: this.env.config.appVersion,
      deviceInfo: () => this.deviceInfo(),
      wsBase: this.env.config.wsBase,
      wsFactory: this.env.wsFactory,
      now: () => this.clock.now(),
      log: (m) => this.env.log('warn', m),
    });
    ov.enableTech((c) => this.techClaim(c), this.env.techMode);
    let failures = 0;
    while (!ctrl.signal.aborted) {
      let req: PairingRequestResponse;
      try {
        req = await flow.createRequest();
        failures = 0;
      } catch (e) {
        failures++;
        const d = backoffDelay(failures, 5_000, 60_000);
        await ov.showPairing({ code: '····-····', qrUrl: null, expiresAtMs: null, now: this.clock.now() });
        ov.setPairingStatus(`Sem conexão com o servidor (${errorMessage(e)}). Nova tentativa em ${Math.round(d / 1000)} s.`);
        await sleep(d, undefined, ctrl.signal).catch(() => undefined);
        continue;
      }
      this.currentPairing = { flow, req };
      const qrUrl = this.env.config.panelUrl
        ? `${this.env.config.panelUrl.replace(/\/+$/, '')}/telas/parear?code=${encodeURIComponent(req.pairing_code)}`
        : req.panel_pair_url || null;
      await ov.showPairing({ code: req.pairing_code, qrUrl, expiresAtMs: req.expires_at_ms, now: this.clock.now() });
      ov.setPairingStatus('Aguardando pareamento…');
      let outcome: 'CLAIMED' | 'EXPIRED';
      try {
        outcome = await flow.waitForClaim(req, ctrl.signal);
      } catch {
        if (ctrl.signal.aborted) return;
        continue;
      }
      if (outcome === 'EXPIRED') continue;
      ov.setPairingStatus('Pareado! Concluindo…');
      let result: PairingResult | null = null;
      for (let i = 0; i < 5 && !result; i++) {
        try {
          result = await flow.fetchResult(req);
        } catch (e) {
          ov.setPairingStatus(`Concluindo pareamento… (${errorMessage(e)})`);
          await sleep(backoffDelay(i, 2_000, 30_000), undefined, ctrl.signal).catch(() => undefined);
        }
      }
      if (!result) continue;
      await this.completePairing(result);
      return;
    }
  }

  private async techClaim(c: TechClaim): Promise<void> {
    const cur = this.currentPairing;
    if (!cur) throw new Error('aguarde o código de pareamento');
    if (!c.login || !c.password || !c.screen_code) throw new Error('preencha login, senha e código da tela');
    const body: { login: string; password: string; screen_code: string; totp?: string } = { login: c.login, password: c.password, screen_code: c.screen_code };
    if (c.totp) body.totp = c.totp;
    await cur.flow.claimOnDevice(cur.req, body);
  }

  private async completePairing(r: PairingResult): Promise<void> {
    const rec: DeviceRecord = {
      device_id: r.device_id,
      screen_id: r.screen_id,
      screen_code: r.screen_code,
      server_keys: r.server_keys.filter((k: ServerKey) => k.alg === 'ES256'),
      paired_at_ms: this.clock.now(),
    };
    await this.env.state.set(STATE_KEYS.device, rec);
    this.device = rec;
    this.currentPairing = null;
    this.revoked = false;
    this.paired = true;
    this.env.overlays.hidePairing();
    this.env.overlays.hideMessage();
    this.env.log('info', `pareado: ${rec.screen_code} (${rec.device_id})`);
    this.markSynced();
    this.engine.refresh();
    this.goOnline();
  }
}
