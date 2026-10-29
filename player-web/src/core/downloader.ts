/**
 * Download de mídia (RDPP §6; P001 §67; A4 Q17):
 *  - `Range: bytes=<offset>-` + `If-Range: "<sha256>"` para retomar; 200 ⇒ servidor ignorou o Range ⇒ recomeça do 0.
 *  - SHA-256 INCREMENTAL por bloco (estado de 32 bytes) — nunca `subtle.digest` do arquivo inteiro, nunca o arquivo
 *    inteiro em memória. Na retomada, o hash é reconstruído relendo o parcial do disco em blocos.
 *  - Arquivo só vira "definitivo" (commit) com tamanho e sha256 conferidos; divergência ⇒ descarta o parcial.
 *  - Fila com 2–3 downloads paralelos, backoff com jitter e falha PERMANENTE após 5 tentativas (≤ 30 min).
 *    Erros de rede (sem resposta HTTP) não contam como tentativa da mídia: é conectividade, não a mídia.
 */

import { createSha256, type IncrementalHash } from './crypto';
import type { MediaStore, MediaWriter } from './storage';
import { abortError, backoffDelay, errorMessage, isAbortError, realTimers, sleep, type Timers } from './util';

export type DownloadErrorKind =
  | 'NETWORK'
  | 'HTTP'
  | 'AUTH'
  | 'NOT_FOUND'
  | 'RANGE'
  | 'SIZE_MISMATCH'
  | 'SHA_MISMATCH'
  | 'QUOTA'
  | 'STORE';

export class DownloadError extends Error {
  constructor(
    public readonly kind: DownloadErrorKind,
    message: string,
    public readonly status: number | null = null,
  ) {
    super(`${kind}: ${message}`);
    this.name = 'DownloadError';
  }
  /** Conta como tentativa da mídia (para a falha permanente)? */
  get counts(): boolean {
    return this.kind !== 'NETWORK' && this.kind !== 'AUTH' && this.kind !== 'QUOTA';
  }
  /** Interrompe a fila inteira (sem espaço em disco). */
  get fatal(): boolean {
    return this.kind === 'QUOTA';
  }
}

export interface DownloadJob {
  /** Chave em `manifest.media`. */
  key: string;
  media_id: string;
  sha256: string;
  size: number;
  mime: string;
}

export type FetchFn = (url: string, init: RequestInit) => Promise<Response>;

export interface DownloadFileDeps {
  fetch: FetchFn;
  url: string;
  headers: Record<string, string>;
  store: MediaStore;
  signal?: AbortSignal;
  onProgress?: (doneBytes: number) => void;
  createHash?: () => IncrementalHash;
}

export interface DownloadFileResult {
  resumedFrom: number;
  bytesFetched: number;
  alreadyPresent: boolean;
}

function isQuotaError(e: unknown): boolean {
  return e instanceof Error && (e.name === 'QuotaExceededError' || /quota/i.test(e.message));
}

const CONTENT_RANGE = /^bytes (\d+)-(\d+)\/(\d+|\*)$/;

/** Baixa (ou retoma) UM arquivo, verificando tamanho e sha256 de forma incremental. */
export async function downloadFile(job: DownloadJob, deps: DownloadFileDeps): Promise<DownloadFileResult> {
  const { store } = deps;
  const sha = job.sha256;
  if (await store.has(sha)) {
    deps.onProgress?.(job.size);
    return { resumedFrom: job.size, bytesFetched: 0, alreadyPresent: true };
  }
  let hash = (deps.createHash ?? createSha256)();
  let offset = await store.partialSize(sha);
  if (offset > job.size) {
    await store.discardPartial(sha);
    offset = 0;
  }
  if (offset > 0) {
    // Reconstrói o estado do hash relendo o parcial em blocos (stream do disco).
    let read = 0;
    for await (const chunk of store.readPartial(sha)) {
      if (deps.signal?.aborted) throw abortError();
      const take = Math.min(chunk.length, offset - read);
      if (take <= 0) break;
      hash.update(take === chunk.length ? chunk : chunk.subarray(0, take));
      read += take;
    }
    if (read !== offset) {
      await store.discardPartial(sha);
      throw new DownloadError('STORE', `parcial ilegível (${read}/${offset})`);
    }
  }
  let resumedFrom = offset;
  let fetched = 0;

  if (offset < job.size) {
    const headers: Record<string, string> = { ...deps.headers };
    if (offset > 0) {
      headers.Range = `bytes=${offset}-`;
      headers['If-Range'] = `"${sha}"`;
    }
    let res: Response;
    try {
      res = await deps.fetch(deps.url, { method: 'GET', headers, signal: deps.signal, cache: 'no-store', credentials: 'omit' });
    } catch (e) {
      if (deps.signal?.aborted || isAbortError(e)) throw abortError();
      throw new DownloadError('NETWORK', errorMessage(e));
    }
    if (res.status === 401) {
      await cancelBody(res);
      throw new DownloadError('AUTH', 'token expirado/inválido', 401);
    }
    if (res.status === 404 || res.status === 410 || res.status === 403) {
      await cancelBody(res);
      throw new DownloadError('NOT_FOUND', `HTTP ${res.status}`, res.status);
    }
    if (res.status === 416) {
      await cancelBody(res);
      await store.discardPartial(sha);
      throw new DownloadError('RANGE', 'Range não satisfatível; parcial descartado', 416);
    }
    if (res.status === 200) {
      if (offset > 0) {
        // Servidor ignorou o Range (ou o ETag mudou): recomeça do zero com hash novo.
        offset = 0;
        resumedFrom = 0;
        hash = (deps.createHash ?? createSha256)();
      }
    } else if (res.status === 206) {
      const m = CONTENT_RANGE.exec(res.headers.get('Content-Range') ?? '');
      if (!m || Number(m[1]) !== offset) {
        await cancelBody(res);
        await store.discardPartial(sha);
        throw new DownloadError('RANGE', `Content-Range inesperado: ${res.headers.get('Content-Range')}`, 206);
      }
    } else {
      await cancelBody(res);
      throw new DownloadError('HTTP', `HTTP ${res.status}`, res.status);
    }
    fetched = await pump(res, offset, hash);
    offset += fetched;
  }

  if (offset < job.size) {
    // Conexão encerrou antes do fim: mantém o parcial para retomar.
    throw new DownloadError('NETWORK', `incompleto (${offset}/${job.size})`);
  }
  const digest = hash.digestHex();
  if (digest !== sha) {
    await store.discardPartial(sha);
    throw new DownloadError('SHA_MISMATCH', `sha256 ${digest} ≠ ${sha}`);
  }
  await store.commit(sha);
  return { resumedFrom, bytesFetched: fetched, alreadyPresent: false };

  async function pump(res: Response, start: number, h: IncrementalHash): Promise<number> {
    if (!res.body) throw new DownloadError('HTTP', 'resposta sem corpo', res.status);
    let writer: MediaWriter;
    try {
      writer = await store.openWriter(sha, start);
    } catch (e) {
      await cancelBody(res);
      if (isQuotaError(e)) throw new DownloadError('QUOTA', errorMessage(e));
      throw new DownloadError('STORE', errorMessage(e));
    }
    const reader = res.body.getReader();
    let pos = start;
    let oversize = false;
    try {
      for (;;) {
        let chunk: ReadableStreamReadResult<Uint8Array>;
        try {
          chunk = await reader.read();
        } catch (e) {
          if (deps.signal?.aborted) throw abortError();
          throw new DownloadError('NETWORK', `leitura interrompida em ${pos}: ${errorMessage(e)}`);
        }
        if (chunk.done) break;
        if (deps.signal?.aborted) {
          await reader.cancel().catch(() => undefined);
          throw abortError();
        }
        const value = chunk.value;
        if (pos + value.length > job.size) {
          oversize = true;
          await reader.cancel().catch(() => undefined);
          break;
        }
        h.update(value);
        try {
          await writer.write(value);
        } catch (e) {
          await reader.cancel().catch(() => undefined);
          if (isQuotaError(e)) throw new DownloadError('QUOTA', errorMessage(e));
          throw new DownloadError('STORE', errorMessage(e));
        }
        pos += value.length;
        deps.onProgress?.(pos);
      }
    } finally {
      await writer.close().catch(() => undefined);
    }
    if (oversize) {
      await store.discardPartial(sha);
      throw new DownloadError('SIZE_MISMATCH', `arquivo maior que ${job.size} bytes`);
    }
    return pos - start;
  }
}

async function cancelBody(res: Response): Promise<void> {
  try {
    await res.body?.cancel();
  } catch {
    /* ignore */
  }
}

// ───────────────────────── Fila ─────────────────────────

export type JobExecutor = (job: DownloadJob, ctx: { signal: AbortSignal; onProgress: (doneBytes: number) => void }) => Promise<void>;

export interface FailedJob {
  job: DownloadJob;
  attempts: number;
  reason: string;
}

export interface QueueResult {
  completed: string[];
  failed: FailedJob[];
  aborted: boolean;
  fatal: DownloadError | null;
}

export interface DownloadQueueOptions {
  executor: JobExecutor;
  /** 2–3 (RDPP §6). */
  concurrency?: number;
  maxAttempts?: number;
  /** Janela da falha permanente. */
  windowMs?: number;
  /** Atrasos base entre tentativas contadas (com jitter ±20 %). Soma ≈ 10,6 min ⇒ 5 tentativas cabem em 30 min. */
  retryDelaysMs?: readonly number[];
  timers?: Timers;
  random?: () => number;
  now?: () => number;
  onProgress?: (p: { done_bytes: number; total_bytes: number }) => void;
  onAttemptFailed?: (job: DownloadJob, err: unknown, attempts: number, permanent: boolean) => void;
}

interface JobState {
  job: DownloadJob;
  attempts: number;
  netFailures: number;
  firstAttemptAt: number | null;
  notBefore: number;
  running: boolean;
  done: boolean;
  failed: boolean;
  lastError: string;
  bytes: number;
}

export const DEFAULT_RETRY_DELAYS_MS: readonly number[] = [5_000, 30_000, 120_000, 480_000];

export class DownloadQueue {
  private readonly o: Required<Omit<DownloadQueueOptions, 'onProgress' | 'onAttemptFailed'>> &
    Pick<DownloadQueueOptions, 'onProgress' | 'onAttemptFailed'>;

  constructor(opts: DownloadQueueOptions) {
    this.o = {
      concurrency: Math.min(3, Math.max(1, opts.concurrency ?? 2)),
      maxAttempts: opts.maxAttempts ?? 5,
      windowMs: opts.windowMs ?? 30 * 60_000,
      retryDelaysMs: opts.retryDelaysMs ?? DEFAULT_RETRY_DELAYS_MS,
      timers: opts.timers ?? realTimers,
      random: opts.random ?? Math.random,
      now: opts.now ?? (() => Date.now()),
      executor: opts.executor,
      onProgress: opts.onProgress,
      onAttemptFailed: opts.onAttemptFailed,
    };
  }

  get concurrency(): number {
    return this.o.concurrency;
  }

  async run(jobs: readonly DownloadJob[], signal: AbortSignal): Promise<QueueResult> {
    const states: JobState[] = jobs.map((job) => ({
      job,
      attempts: 0,
      netFailures: 0,
      firstAttemptAt: null,
      notBefore: 0,
      running: false,
      done: false,
      failed: false,
      lastError: '',
      bytes: 0,
    }));
    const total = jobs.reduce((n, j) => n + j.size, 0);
    let fatal: DownloadError | null = null;
    const inner = new AbortController();
    const onAbort = (): void => inner.abort();
    signal.addEventListener('abort', onAbort, { once: true });
    const report = (): void => this.o.onProgress?.({ done_bytes: states.reduce((n, s) => n + s.bytes, 0), total_bytes: total });

    const worker = async (): Promise<void> => {
      for (;;) {
        if (inner.signal.aborted) return;
        const now = this.o.now();
        const ready = states.find((s) => !s.done && !s.failed && !s.running && s.notBefore <= now);
        if (!ready) {
          const waiting = states.filter((s) => !s.done && !s.failed && !s.running);
          if (waiting.length === 0) return;
          const next = Math.min(...waiting.map((s) => s.notBefore));
          try {
            await sleep(Math.max(1, next - now), this.o.timers, inner.signal);
          } catch {
            return;
          }
          continue;
        }
        ready.running = true;
        ready.firstAttemptAt ??= now;
        try {
          await this.o.executor(ready.job, {
            signal: inner.signal,
            onProgress: (b) => {
              ready.bytes = b;
              report();
            },
          });
          ready.done = true;
          ready.bytes = ready.job.size;
          report();
        } catch (e) {
          if (inner.signal.aborted || isAbortError(e)) {
            ready.running = false;
            return;
          }
          const err = e instanceof DownloadError ? e : new DownloadError('STORE', errorMessage(e));
          ready.lastError = err.message;
          if (err.fatal) {
            fatal = err;
            inner.abort();
            ready.running = false;
            return;
          }
          const t = this.o.now();
          let permanent = false;
          if (err.counts) {
            ready.attempts++;
            const elapsed = t - (ready.firstAttemptAt ?? t);
            permanent = ready.attempts >= this.o.maxAttempts || (elapsed >= this.o.windowMs && ready.attempts >= 2);
            if (permanent) {
              ready.failed = true;
            } else {
              const base = this.o.retryDelaysMs[Math.min(ready.attempts - 1, this.o.retryDelaysMs.length - 1)] ?? 5_000;
              ready.notBefore = t + Math.floor(base * (0.8 + 0.4 * this.o.random()));
            }
          } else {
            ready.netFailures++;
            ready.notBefore = t + backoffDelay(ready.netFailures, err.kind === 'AUTH' ? 500 : 2_000, 60_000, this.o.random);
          }
          this.o.onAttemptFailed?.(ready.job, err, ready.attempts, permanent);
        }
        ready.running = false;
      }
    };

    try {
      await Promise.all(Array.from({ length: Math.min(this.o.concurrency, Math.max(1, states.length)) }, worker));
    } finally {
      signal.removeEventListener('abort', onAbort);
    }
    return {
      completed: states.filter((s) => s.done).map((s) => s.job.key),
      failed: states.filter((s) => s.failed).map((s) => ({ job: s.job, attempts: s.attempts, reason: s.lastError })),
      aborted: signal.aborted,
      fatal,
    };
  }
}
