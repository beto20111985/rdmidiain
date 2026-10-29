/**
 * Executor de downloads no Web Worker (thread principal livre para a reprodução).
 * Se o Worker/OPFS-sync não estiver disponível, o app usa o executor na thread principal.
 */

import { DownloadError, type DownloadErrorKind, type DownloadJob, type JobExecutor } from '../../core/downloader';
import { abortError, uuidv4 } from '../../core/util';
import { trustedScriptUrl } from './trusted';

type OutMsg =
  | { type: 'probe'; ok: boolean }
  | { type: 'progress'; id: string; done: number }
  | { type: 'done'; id: string }
  | { type: 'aborted'; id: string }
  | { type: 'error'; id: string; kind: DownloadErrorKind; message: string; status: number | null };

interface Pending {
  resolve: () => void;
  reject: (e: unknown) => void;
  onProgress: (done: number) => void;
}

export class WorkerDownloadExecutor {
  private readonly pending = new Map<string, Pending>();

  private constructor(private readonly worker: Worker) {
    worker.addEventListener('message', (ev: MessageEvent) => this.onMessage(ev.data as OutMsg));
    worker.addEventListener('error', () => {
      for (const p of this.pending.values()) p.reject(new DownloadError('STORE', 'worker de download falhou'));
      this.pending.clear();
    });
  }

  /** Cria o worker e verifica OPFS + SyncAccessHandle; null se indisponível. */
  static async create(scriptUrl: string, timeoutMs = 4_000): Promise<WorkerDownloadExecutor | null> {
    if (typeof Worker === 'undefined') return null;
    let worker: Worker;
    try {
      worker = new Worker(trustedScriptUrl(scriptUrl), { type: import.meta.env.DEV ? 'module' : 'classic', name: 'rd-download' });
    } catch {
      return null;
    }
    const ok = await new Promise<boolean>((resolve) => {
      const t = setTimeout(() => resolve(false), timeoutMs);
      const onMsg = (ev: MessageEvent): void => {
        const d = ev.data as OutMsg;
        if (d?.type === 'probe') {
          clearTimeout(t);
          worker.removeEventListener('message', onMsg);
          resolve(d.ok);
        }
      };
      worker.addEventListener('message', onMsg);
      worker.addEventListener('error', () => {
        clearTimeout(t);
        resolve(false);
      });
      worker.postMessage({ type: 'probe' });
    });
    if (!ok) {
      worker.terminate();
      return null;
    }
    return new WorkerDownloadExecutor(worker);
  }

  executor(urlFor: (job: DownloadJob) => string, headers: () => Promise<Record<string, string>>): JobExecutor {
    return async (job, ctx) => {
      if (ctx.signal.aborted) throw abortError();
      const h = await headers();
      const id = uuidv4();
      await new Promise<void>((resolve, reject) => {
        const onAbort = (): void => this.worker.postMessage({ type: 'abort', id });
        ctx.signal.addEventListener('abort', onAbort, { once: true });
        this.pending.set(id, {
          resolve: () => {
            ctx.signal.removeEventListener('abort', onAbort);
            resolve();
          },
          reject: (e) => {
            ctx.signal.removeEventListener('abort', onAbort);
            reject(e);
          },
          onProgress: ctx.onProgress,
        });
        this.worker.postMessage({ type: 'job', id, job, url: urlFor(job), headers: h });
      });
    };
  }

  terminate(): void {
    this.worker.terminate();
  }

  private onMessage(d: OutMsg): void {
    if (!d || d.type === 'probe') return;
    const p = this.pending.get(d.id);
    if (!p) return;
    switch (d.type) {
      case 'progress':
        p.onProgress(d.done);
        break;
      case 'done':
        this.pending.delete(d.id);
        p.resolve();
        break;
      case 'aborted':
        this.pending.delete(d.id);
        p.reject(abortError());
        break;
      case 'error':
        this.pending.delete(d.id);
        p.reject(new DownloadError(d.kind, d.message.replace(/^[A-Z_]+: /, ''), d.status));
        break;
    }
  }
}
