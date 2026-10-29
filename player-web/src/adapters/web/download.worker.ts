/**
 * Web Worker de download: executa o MESMO `downloadFile` do núcleo (Range/If-Range, SHA-256 incremental)
 * gravando no OPFS por FileSystemSyncAccessHandle (escrita no lugar, flush periódico).
 * Protocolo com a thread principal:
 *   → {type:"probe"}                                   ← {type:"probe", ok}
 *   → {type:"job", id, job, url, headers}              ← {type:"progress", id, done} … {type:"done", id}
 *                                                      ← {type:"error", id, kind, message, status} | {type:"aborted", id}
 *   → {type:"abort", id}
 * O token (Bearer) só existe em memória, no cabeçalho desta requisição.
 */

import { DownloadError, downloadFile, type DownloadJob } from '../../core/downloader';
import { errorMessage, isAbortError } from '../../core/util';
import { OpfsMediaStore } from './opfsMediaStore';
import { mediaDir } from './opfsTypes';

interface WorkerScope {
  postMessage(msg: unknown): void;
  addEventListener(type: 'message', fn: (ev: MessageEvent) => void): void;
}

type InMsg =
  | { type: 'probe' }
  | { type: 'job'; id: string; job: DownloadJob; url: string; headers: Record<string, string> }
  | { type: 'abort'; id: string };

const scope = globalThis as unknown as WorkerScope;
const storeP = OpfsMediaStore.open('sync');
const running = new Map<string, AbortController>();

async function probe(): Promise<boolean> {
  try {
    const dir = await mediaDir();
    if (!dir || !(await storeP)) return false;
    const fh = await dir.getFileHandle('.probe', { create: true });
    let ok = false;
    if (typeof fh.createSyncAccessHandle === 'function') {
      const h = await fh.createSyncAccessHandle();
      h.close();
      ok = true;
    }
    await dir.removeEntry('.probe').catch(() => undefined);
    return ok;
  } catch {
    return false;
  }
}

async function run(id: string, job: DownloadJob, url: string, headers: Record<string, string>): Promise<void> {
  const ctrl = new AbortController();
  running.set(id, ctrl);
  let lastPost = 0;
  let lastBytes = 0;
  try {
    const store = await storeP;
    if (!store) throw new DownloadError('STORE', 'OPFS indisponível no worker');
    await downloadFile(job, {
      fetch: (u, init) => fetch(u, init),
      url,
      headers,
      store,
      signal: ctrl.signal,
      onProgress: (done) => {
        const now = Date.now();
        if (now - lastPost >= 250 || done - lastBytes >= 1024 * 1024 || done === job.size) {
          lastPost = now;
          lastBytes = done;
          scope.postMessage({ type: 'progress', id, done });
        }
      },
    });
    scope.postMessage({ type: 'done', id });
  } catch (e) {
    if (ctrl.signal.aborted || isAbortError(e)) {
      scope.postMessage({ type: 'aborted', id });
    } else if (e instanceof DownloadError) {
      scope.postMessage({ type: 'error', id, kind: e.kind, message: e.message, status: e.status });
    } else {
      scope.postMessage({ type: 'error', id, kind: 'STORE', message: errorMessage(e), status: null });
    }
  } finally {
    running.delete(id);
  }
}

scope.addEventListener('message', (ev: MessageEvent) => {
  const msg = ev.data as InMsg;
  if (!msg || typeof msg !== 'object') return;
  switch (msg.type) {
    case 'probe':
      void probe().then((ok) => scope.postMessage({ type: 'probe', ok }));
      break;
    case 'job':
      void run(msg.id, msg.job, msg.url, msg.headers);
      break;
    case 'abort':
      running.get(msg.id)?.abort();
      break;
  }
});
