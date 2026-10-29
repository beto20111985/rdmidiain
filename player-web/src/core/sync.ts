/**
 * Sincronização e atualização ATÔMICA do manifesto (RDPP §6; ETAPA2 #278–#281, #1226–#1236; P001 §66, §68).
 *
 *   RECEIVED → VERIFY_SIG → PLAN → DOWNLOADING → VERIFY_SHA → STAGED → COMMIT → ACTIVE   (falha ⇒ mantém a ativa)
 *
 *  - Assinatura ES256 do servidor verificada com a chave do `key_id` recebida no pareamento (manifesto adulterado
 *    é rejeitado). `screen_id` precisa bater; versão ≤ ativa/staged é ignorada (sem regressão por replay).
 *  - PLAN: só baixa o que falta; consulta a quota (`estimate`) antes; GC se necessário; sem espaço ⇒ DISK_FULL.
 *  - Falha PERMANENTE de uma mídia (5 tentativas ≤ 30 min, ver downloader) ⇒ a versão é ativada SEM ela
 *    (entradas viram fallback) e `media_error` é emitido (ADR_007 §8).
 *  - STAGED fica persistido; o COMMIT (troca atômica de ponteiro numa transação) é feito pelo runtime de reprodução
 *    na fronteira de ciclo (fase == 0). Sem versão ativa (primeira execução) a ativação é imediata.
 *  - Mantém a versão anterior para rollback; GC remove só mídias fora de {ativa, staged, anterior} ou vencidas.
 */

import { importSpkiVerifier, verifyCanonical, type Verifier } from './crypto';
import type { DownloadJob, DownloadQueue } from './downloader';
import { validateSignedManifest, type Manifest, type ServerKey, type SignedManifest } from './protocol';
import { STATE_KEYS, type MediaStore, type StateStore } from './storage';
import { errorMessage, Mutex } from './util';

export type SyncState =
  | 'IDLE'
  | 'RECEIVED'
  | 'VERIFY_SIG'
  | 'PLAN'
  | 'DOWNLOADING'
  | 'VERIFY_SHA'
  | 'STAGED'
  | 'COMMIT'
  | 'ACTIVE'
  | 'FAILED';

export interface StoredManifest {
  signed: SignedManifest;
  version: number;
  /** Chaves de mídia com falha permanente (as entradas viram fallback). */
  failed_media: string[];
  received_at_ms: number;
  staged_at_ms: number | null;
  activated_at_ms: number | null;
}

export type SyncEvent =
  | { type: 'state'; state: SyncState; version: number | null }
  | { type: 'progress'; done_bytes: number; total_bytes: number }
  | { type: 'staged'; version: number }
  | { type: 'manifest_applied'; version: number }
  | { type: 'manifest_failed'; version: number | null; reason: string }
  | { type: 'media_error'; media_id: string; reason: string; attempts: number }
  | { type: 'media_ready'; keys: string[] }
  | { type: 'disk_full'; needed_bytes: number; free_bytes: number | null };

export type ReceiveOutcome = 'STAGED' | 'ACTIVATED' | 'NOOP' | 'SUPERSEDED' | 'REJECTED' | 'FAILED';

export interface StorageEstimate {
  quota: number;
  usage: number;
}

export interface SyncEngineDeps {
  store: StateStore;
  media: MediaStore;
  queue: DownloadQueue;
  serverKeys: () => readonly ServerKey[];
  screenId: () => string | null;
  estimate?: () => Promise<StorageEstimate | null>;
  now: () => number;
  emit: (e: SyncEvent) => void;
  log?: (msg: string) => void;
  /** Folga exigida além dos bytes a baixar (padrão 64 MiB). */
  reserveBytes?: number;
}

export class SyncRejected extends Error {
  constructor(
    public readonly reason: string,
    message: string,
  ) {
    super(`${reason}: ${message}`);
    this.name = 'SyncRejected';
  }
}

export class SyncEngine {
  private _state: SyncState = 'IDLE';
  private active: StoredManifest | null = null;
  private staged: StoredManifest | null = null;
  private previous: StoredManifest | null = null;
  private present = new Set<string>();
  private gen = 0;
  private ctrl: AbortController | null = null;
  private inflightVersion: number | null = null;
  private readonly commitMutex = new Mutex();
  private verifiers = new Map<string, Promise<Verifier>>();
  private repairCtrl: AbortController | null = null;
  progress: { done_bytes: number; total_bytes: number } | null = null;

  constructor(private readonly deps: SyncEngineDeps) {}

  get state(): SyncState {
    return this._state;
  }

  /** Restaura ponteiros persistidos e o inventário de mídia. */
  async load(): Promise<void> {
    const s = this.deps.store;
    this.active = (await s.get<StoredManifest>(STATE_KEYS.manifestActive)) ?? null;
    this.staged = (await s.get<StoredManifest>(STATE_KEYS.manifestStaged)) ?? null;
    this.previous = (await s.get<StoredManifest>(STATE_KEYS.manifestPrevious)) ?? null;
    this.present = new Set(await this.deps.media.list());
    if (this.staged) {
      // Staged só vale se tudo (exceto falhas permanentes) continua presente.
      const m = this.staged.signed.manifest;
      const missing = Object.entries(m.media).filter(
        ([k, e]) => !this.staged!.failed_media.includes(k) && !this.present.has(e.sha256),
      );
      if (missing.length || (this.active && this.staged.version <= this.active.version)) {
        this.staged = null;
        await s.delete(STATE_KEYS.manifestStaged);
      }
    }
    this.setState(this.active ? 'ACTIVE' : 'IDLE', this.active?.version ?? null);
  }

  activeManifest(): StoredManifest | null {
    return this.active;
  }
  stagedManifest(): StoredManifest | null {
    return this.staged;
  }
  previousManifest(): StoredManifest | null {
    return this.previous;
  }
  hasStaged(): boolean {
    return this.staged !== null;
  }
  get pendingVersion(): number | null {
    return this.staged?.version ?? this.inflightVersion;
  }
  /** Maior versão conhecida (para `If-None-Match` e para ignorar `sync_required` antigos). */
  knownVersion(): number | null {
    const vs = [this.active?.version, this.staged?.version, this.inflightVersion].filter((v): v is number => typeof v === 'number');
    return vs.length ? Math.max(...vs) : null;
  }

  /** Chaves de mídia do manifesto presentes e verificadas. */
  mediaReadyKeys(m: Manifest): string[] {
    return Object.entries(m.media)
      .filter(([, e]) => this.present.has(e.sha256))
      .map(([k]) => k);
  }

  isPresent(sha256: string): boolean {
    return this.present.has(sha256);
  }

  /** Processa um manifesto assinado recebido do servidor. */
  async receive(raw: unknown): Promise<ReceiveOutcome> {
    this.setState('RECEIVED', null);
    let signed: SignedManifest;
    try {
      validateSignedManifest(raw);
      signed = raw;
    } catch (e) {
      return this.reject(null, 'MANIFEST_INVALID', errorMessage(e));
    }
    const m = signed.manifest;
    this.setState('VERIFY_SIG', m.version);
    const key = this.deps.serverKeys().find((k) => k.key_id === signed.key_id);
    if (!key) return this.reject(m.version, 'UNKNOWN_KEY', `key_id desconhecido: ${signed.key_id}`);
    let ok = false;
    try {
      ok = await verifyCanonical(await this.verifier(key), m, signed.signature);
    } catch (e) {
      return this.reject(m.version, 'BAD_SIGNATURE', errorMessage(e));
    }
    if (!ok) return this.reject(m.version, 'BAD_SIGNATURE', 'assinatura do manifesto inválida');
    const screenId = this.deps.screenId();
    if (screenId && m.screen_id !== screenId) {
      return this.reject(m.version, 'SCREEN_MISMATCH', `manifesto da tela ${m.screen_id}`);
    }
    if (this.active && m.version <= this.active.version) return this.noop();
    if (this.staged && m.version <= this.staged.version) return this.noop();
    if (this.inflightVersion !== null && m.version <= this.inflightVersion) return 'NOOP';

    // Nova versão: cancela um download em andamento de versão mais antiga (parciais ficam para retomar).
    const myGen = ++this.gen;
    this.ctrl?.abort();
    this.repairCtrl?.abort(); // evita dois downloads do mesmo arquivo em paralelo
    const ctrl = new AbortController();
    this.ctrl = ctrl;
    this.inflightVersion = m.version;
    try {
      this.setState('PLAN', m.version);
      const jobs = this.planJobs(m);
      const needed = jobs.reduce((n, j) => n + j.size, 0);
      if (needed > 0 && !(await this.ensureSpace(needed, m))) {
        return this.fail(m.version, 'DISK_FULL', `sem espaço para ${needed} bytes`);
      }
      this.setState('DOWNLOADING', m.version);
      this.progress = { done_bytes: 0, total_bytes: needed };
      const result = await this.deps.queue.run(jobs, ctrl.signal);
      if (myGen !== this.gen || result.aborted) return 'SUPERSEDED';
      if (result.fatal) {
        this.deps.emit({ type: 'disk_full', needed_bytes: needed, free_bytes: null });
        return this.fail(m.version, 'DISK_FULL', result.fatal.message);
      }
      for (const key of result.completed) {
        const e = m.media[key];
        if (e) this.present.add(e.sha256);
      }
      this.setState('VERIFY_SHA', m.version);
      const failed: string[] = [];
      for (const f of result.failed) {
        failed.push(f.job.key);
        this.deps.emit({ type: 'media_error', media_id: f.job.media_id, reason: f.reason, attempts: f.attempts });
      }
      // Confirma no armazenamento que tudo o que não falhou está presente (hash já conferido no download).
      for (const [key, e] of Object.entries(m.media)) {
        if (failed.includes(key)) continue;
        if (!this.present.has(e.sha256)) {
          if (await this.deps.media.has(e.sha256)) this.present.add(e.sha256);
          else failed.push(key);
        }
      }
      if (myGen !== this.gen) return 'SUPERSEDED';
      const record: StoredManifest = {
        signed,
        version: m.version,
        failed_media: failed,
        received_at_ms: this.deps.now(),
        staged_at_ms: this.deps.now(),
        activated_at_ms: null,
      };
      await this.deps.store.set(STATE_KEYS.manifestStaged, record);
      this.staged = record;
      this.setState('STAGED', m.version);
      this.deps.emit({ type: 'staged', version: m.version });
      if (!this.active) {
        await this.commit();
        return 'ACTIVATED';
      }
      return 'STAGED';
    } catch (e) {
      if (myGen !== this.gen) return 'SUPERSEDED';
      return this.fail(m.version, 'SYNC_ERROR', errorMessage(e));
    } finally {
      if (myGen === this.gen) {
        this.inflightVersion = null;
        this.ctrl = null;
        this.progress = null;
      }
    }
  }

  /** COMMIT → ACTIVE: troca atômica de ponteiro (staged→ativa, ativa→anterior). */
  commit(): Promise<StoredManifest | null> {
    return this.commitMutex.run(async () => {
      const staged = this.staged;
      if (!staged) return null;
      this.setState('COMMIT', staged.version);
      const now = this.deps.now();
      const nextActive: StoredManifest = { ...staged, activated_at_ms: now };
      const set: Record<string, unknown> = { [STATE_KEYS.manifestActive]: nextActive };
      if (this.active) set[STATE_KEYS.manifestPrevious] = this.active;
      try {
        await this.deps.store.transact({ set, del: [STATE_KEYS.manifestStaged] });
      } catch (e) {
        // Falha no commit: nada mudou (transação), mantém a ativa.
        this.setState(this.active ? 'ACTIVE' : 'FAILED', this.active?.version ?? null);
        this.deps.emit({ type: 'manifest_failed', version: staged.version, reason: `COMMIT_FAILED: ${errorMessage(e)}` });
        return null;
      }
      if (this.active) this.previous = this.active;
      this.active = nextActive;
      this.staged = null;
      this.setState('ACTIVE', nextActive.version);
      this.deps.emit({ type: 'manifest_applied', version: nextActive.version });
      void this.gc().catch((e) => this.deps.log?.(`gc: ${errorMessage(e)}`));
      return nextActive;
    });
  }

  /** Volta para a versão anterior (falha ao aplicar a nova). */
  rollback(reason: string): Promise<boolean> {
    return this.commitMutex.run(async () => {
      if (!this.previous || !this.active) return false;
      const failedVersion = this.active.version;
      const restored = this.previous;
      await this.deps.store.transact({
        set: { [STATE_KEYS.manifestActive]: restored },
        del: [STATE_KEYS.manifestPrevious],
      });
      this.active = restored;
      this.previous = null;
      this.setState('ACTIVE', restored.version);
      this.deps.emit({ type: 'manifest_failed', version: failedVersion, reason: `ROLLBACK: ${reason}` });
      return true;
    });
  }

  /** Tenta novamente baixar as mídias faltantes da versão ativa (após reconectar). */
  async repair(): Promise<string[]> {
    const active = this.active;
    if (!active || this.repairCtrl || this.inflightVersion !== null) return [];
    const m = active.signed.manifest;
    const jobs = this.planJobs(m);
    if (jobs.length === 0) return [];
    const ctrl = new AbortController();
    this.repairCtrl = ctrl;
    try {
      const result = await this.deps.queue.run(jobs, ctrl.signal);
      if (result.aborted) return [];
      const ready: string[] = [];
      for (const key of result.completed) {
        const e = m.media[key];
        if (e) {
          this.present.add(e.sha256);
          ready.push(key);
        }
      }
      if (ready.length) {
        if (this.active?.version === active.version) {
          const updated: StoredManifest = { ...this.active, failed_media: this.active.failed_media.filter((k) => !ready.includes(k)) };
          await this.deps.store.set(STATE_KEYS.manifestActive, updated);
          this.active = updated;
        }
        this.deps.emit({ type: 'media_ready', keys: ready });
      }
      for (const f of result.failed) {
        this.deps.emit({ type: 'media_error', media_id: f.job.media_id, reason: f.reason, attempts: f.attempts });
      }
      return ready;
    } finally {
      if (this.repairCtrl === ctrl) this.repairCtrl = null;
    }
  }

  /** Limpa manifestos e mídia (revogação / clear_cache). */
  async reset(opts: { media: boolean }): Promise<void> {
    this.gen++;
    this.ctrl?.abort();
    this.ctrl = null;
    this.inflightVersion = null;
    await this.deps.store.transact({
      del: [STATE_KEYS.manifestActive, STATE_KEYS.manifestStaged, STATE_KEYS.manifestPrevious],
    });
    this.active = this.staged = this.previous = null;
    if (opts.media) {
      for (const sha of await this.deps.media.list()) await this.deps.media.remove(sha);
      this.present.clear();
    }
    this.setState('IDLE', null);
  }

  /** Remove mídias fora de {ativa, staged, anterior, em download} ou cujos criativos já venceram. */
  async gc(): Promise<string[]> {
    const now = this.deps.now();
    const keep = new Set<string>();
    const manifests = [this.active, this.staged, this.previous].filter((x): x is StoredManifest => x !== null);
    for (const sm of manifests) {
      const m = sm.signed.manifest;
      const live = new Set<string>();
      for (const c of Object.values(m.creatives)) if (c.valid_until_ms > now) live.add(c.media);
      for (const [key, e] of Object.entries(m.media)) {
        // Mídia sem criativo algum (defensivo) é mantida; vencida = todos os criativos que a usam venceram.
        const used = Object.values(m.creatives).some((c) => c.media === key);
        if (!used || live.has(key)) keep.add(e.sha256);
      }
    }
    const removed: string[] = [];
    for (const sha of await this.deps.media.list()) {
      if (!keep.has(sha)) {
        await this.deps.media.remove(sha);
        this.present.delete(sha);
        removed.push(sha);
      }
    }
    return removed;
  }

  private planJobs(m: Manifest): DownloadJob[] {
    const seen = new Set<string>();
    const jobs: DownloadJob[] = [];
    for (const [key, e] of Object.entries(m.media)) {
      if (this.present.has(e.sha256) || seen.has(e.sha256)) continue;
      seen.add(e.sha256);
      jobs.push({ key, media_id: e.media_id, sha256: e.sha256, size: e.size, mime: e.mime });
    }
    return jobs;
  }

  private async ensureSpace(needed: number, incoming: Manifest): Promise<boolean> {
    if (!this.deps.estimate) return true;
    const reserve = this.deps.reserveBytes ?? 64 * 1024 * 1024;
    const free = async (): Promise<number | null> => {
      const est = await this.deps.estimate!().catch(() => null);
      return est ? est.quota - est.usage : null;
    };
    let f = await free();
    if (f === null || f >= needed + reserve) return true;
    // Libera o que não é mais necessário (mantém o que a nova versão reaproveita).
    const incomingShas = new Set(Object.values(incoming.media).map((e) => e.sha256));
    for (const sha of await this.deps.media.list()) {
      if (incomingShas.has(sha)) continue;
      const inUse = [this.active, this.staged].some((sm) => sm && Object.values(sm.signed.manifest.media).some((e) => e.sha256 === sha));
      if (!inUse) {
        await this.deps.media.remove(sha);
        this.present.delete(sha);
      }
    }
    f = await free();
    if (f === null || f >= needed + reserve) return true;
    this.deps.emit({ type: 'disk_full', needed_bytes: needed, free_bytes: f });
    return false;
  }

  private verifier(key: ServerKey): Promise<Verifier> {
    let v = this.verifiers.get(key.key_id + ':' + key.public_key);
    if (!v) {
      v = importSpkiVerifier(key.public_key);
      this.verifiers.set(key.key_id + ':' + key.public_key, v);
      v.catch(() => this.verifiers.delete(key.key_id + ':' + key.public_key));
    }
    return v;
  }

  private noop(): ReceiveOutcome {
    this.setState(this.staged ? 'STAGED' : this.active ? 'ACTIVE' : 'IDLE', this.staged?.version ?? this.active?.version ?? null);
    return 'NOOP';
  }

  private reject(version: number | null, reason: string, msg: string): ReceiveOutcome {
    this.deps.log?.(`manifesto rejeitado (${reason}): ${msg}`);
    this.deps.emit({ type: 'manifest_failed', version, reason });
    this.restoreState();
    return 'REJECTED';
  }

  private fail(version: number, reason: string, msg: string): ReceiveOutcome {
    this.deps.log?.(`sincronização falhou (${reason}): ${msg}`);
    this.deps.emit({ type: 'manifest_failed', version, reason });
    this.restoreState();
    return 'FAILED';
  }

  private restoreState(): void {
    if (this.staged) this.setState('STAGED', this.staged.version);
    else if (this.active) this.setState('ACTIVE', this.active.version);
    else this.setState('FAILED', null);
  }

  private setState(state: SyncState, version: number | null): void {
    this._state = state;
    this.deps.emit({ type: 'state', state, version });
  }
}
