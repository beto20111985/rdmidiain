import { createHash } from 'node:crypto';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DownloadError, DownloadQueue, downloadFile, type DownloadJob } from '../src/core/downloader';
import { MemoryMediaStore } from '../src/core/memoryStores';
import type { MediaStore, MediaWriter } from '../src/core/storage';
import { bytesOf, fakeMediaServer, shaHex } from './helpers/fakeServer';

function job(id: string, data: Uint8Array): DownloadJob {
  return { key: id, media_id: id, sha256: shaHex(data), size: data.length, mime: 'video/mp4' };
}

describe('downloadFile — Range/If-Range, retomada e SHA-256 incremental', () => {
  it('baixa, confere sha256 e só então faz commit', async () => {
    const data = bytesOf(300_000);
    const srv = fakeMediaServer(new Map([['m1', data]]));
    const store = new MemoryMediaStore();
    const r = await downloadFile(job('m1', data), { fetch: srv.fetch, url: '/media/m1', headers: { Authorization: 'Bearer t' }, store });
    expect(r).toEqual({ resumedFrom: 0, bytesFetched: 300_000, alreadyPresent: false });
    expect(await store.has(shaHex(data))).toBe(true);
    expect(store.files.get(shaHex(data))).toEqual(data);
    expect(srv.requests[0]!.headers.authorization).toBe('Bearer t');
    expect(srv.requests[0]!.headers.range).toBeUndefined();
  });

  it('retomada por offset após queda: Range: bytes=<offset>- e If-Range: "<sha256>" (206)', async () => {
    const data = bytesOf(1_000_000, 7);
    const srv = fakeMediaServer(new Map([['m1', data]]), { cutAfter: (i) => (i === 0 ? 400_000 : null), chunk: 50_000 });
    const store = new MemoryMediaStore();
    const j = job('m1', data);
    const deps = { fetch: srv.fetch, url: '/media/m1', headers: {}, store };
    const first = await downloadFile(j, deps).catch((e: unknown) => e);
    expect(first).toBeInstanceOf(DownloadError);
    expect((first as DownloadError).kind).toBe('NETWORK');
    expect((first as DownloadError).counts).toBe(false);
    expect(await store.partialSize(j.sha256)).toBe(400_000);
    expect(await store.has(j.sha256)).toBe(false);

    const second = await downloadFile(j, deps);
    expect(second.resumedFrom).toBe(400_000);
    expect(second.bytesFetched).toBe(600_000);
    expect(srv.requests[1]!.headers.range).toBe('bytes=400000-');
    expect(srv.requests[1]!.headers['if-range']).toBe(`"${j.sha256}"`);
    expect(srv.requests[1]!.status).toBe(206);
    expect(store.files.get(j.sha256)).toEqual(data);
  });

  it('servidor ignora o Range (200) ⇒ recomeça do zero com hash novo', async () => {
    const data = bytesOf(200_000, 3);
    const store = new MemoryMediaStore();
    const j = job('m1', data);
    const w = await store.openWriter(j.sha256, 0);
    await w.write(data.slice(0, 50_000));
    await w.close();
    const srv = fakeMediaServer(new Map([['m1', data]]), { ignoreRange: true });
    const r = await downloadFile(j, { fetch: srv.fetch, url: '/media/m1', headers: {}, store });
    expect(srv.requests[0]!.headers.range).toBe('bytes=50000-');
    expect(srv.requests[0]!.status).toBe(200);
    expect(r.resumedFrom).toBe(0);
    expect(store.files.get(j.sha256)).toEqual(data);
  });

  it('parcial corrompido (sha divergente) ⇒ SHA_MISMATCH, descarta o parcial, não faz commit', async () => {
    const data = bytesOf(100_000, 9);
    const store = new MemoryMediaStore();
    const j = job('m1', data);
    const w = await store.openWriter(j.sha256, 0);
    const bad = data.slice(0, 30_000);
    bad[10] = bad[10]! ^ 0xff;
    await w.write(bad);
    const srv = fakeMediaServer(new Map([['m1', data]]));
    const e = await downloadFile(j, { fetch: srv.fetch, url: '/media/m1', headers: {}, store }).catch((x: unknown) => x);
    expect((e as DownloadError).kind).toBe('SHA_MISMATCH');
    expect((e as DownloadError).counts).toBe(true);
    expect(await store.has(j.sha256)).toBe(false);
    expect(await store.partialSize(j.sha256)).toBe(0);
    // próxima tentativa recomeça do zero e conclui
    await downloadFile(j, { fetch: srv.fetch, url: '/media/m1', headers: {}, store });
    expect(store.files.get(j.sha256)).toEqual(data);
  });

  it('arquivo maior que o declarado ⇒ SIZE_MISMATCH', async () => {
    const data = bytesOf(10_000);
    const j = { ...job('m1', data), size: 9_000 };
    const srv = fakeMediaServer(new Map([['m1', data]]));
    const store = new MemoryMediaStore();
    const e = await downloadFile(j, { fetch: srv.fetch, url: '/media/m1', headers: {}, store }).catch((x: unknown) => x);
    expect((e as DownloadError).kind).toBe('SIZE_MISMATCH');
    expect(await store.partialSize(j.sha256)).toBe(0);
  });

  it('401 ⇒ AUTH (não conta), 404 ⇒ NOT_FOUND (conta), 500 ⇒ HTTP (conta)', async () => {
    const data = bytesOf(1000);
    const store = new MemoryMediaStore();
    const mk = (status: number) => fakeMediaServer(new Map([['m1', data]]), { statusFor: () => status });
    const e401 = (await downloadFile(job('m1', data), { fetch: mk(401).fetch, url: '/m1', headers: {}, store }).catch((x) => x)) as DownloadError;
    const e500 = (await downloadFile(job('m1', data), { fetch: mk(500).fetch, url: '/m1', headers: {}, store }).catch((x) => x)) as DownloadError;
    const e404 = (await downloadFile(job('m1', data), { fetch: fakeMediaServer(new Map()).fetch, url: '/m1', headers: {}, store }).catch((x) => x)) as DownloadError;
    expect([e401.kind, e401.counts]).toEqual(['AUTH', false]);
    expect([e500.kind, e500.counts]).toEqual(['HTTP', true]);
    expect([e404.kind, e404.counts]).toEqual(['NOT_FOUND', true]);
  });

  it('arquivo grande (128 MiB) com hash incremental SEM carregar o arquivo inteiro em memória', async () => {
    const SIZE = 128 * 1024 * 1024;
    const CHUNK = 1024 * 1024;
    const chunkAt = (i: number): Uint8Array => {
      const c = new Uint8Array(CHUNK);
      const v = new DataView(c.buffer);
      for (let o = 0; o < CHUNK; o += 4) v.setUint32(o, (i * 2654435761 + o) >>> 0);
      return c;
    };
    const expected = createHash('sha256');
    for (let i = 0; i < SIZE / CHUNK; i++) expected.update(chunkAt(i));
    const sha = expected.digest('hex');

    // Armazenamento "sumidouro": só conta bytes (como um arquivo em disco), não guarda nada em RAM.
    let written = 0;
    let committed = false;
    const sink: MediaStore = {
      kind: 'sink',
      partialSize: async () => 0,
      openWriter: async (): Promise<MediaWriter> => ({ write: async (c) => void (written += c.length), close: async () => undefined }),
      readPartial: async function* () {},
      discardPartial: async () => undefined,
      commit: async () => void (committed = true),
      has: async () => false,
      list: async () => [],
      remove: async () => undefined,
      getUrl: async () => 'x',
      releaseUrl: () => undefined,
    };
    let i = 0;
    const body = new ReadableStream<Uint8Array>({
      pull(ctrl) {
        if (i >= SIZE / CHUNK) return ctrl.close();
        ctrl.enqueue(chunkAt(i++));
      },
    });
    const gc = (globalThis as { gc?: () => void }).gc;
    expect(gc, 'vitest deve rodar com --expose-gc').toBeTypeOf('function');
    gc!();
    const base = process.memoryUsage().arrayBuffers;
    let peak = 0;
    let lastSample = 0;
    const r = await downloadFile(
      { key: 'big', media_id: 'big', sha256: sha, size: SIZE, mime: 'video/mp4' },
      {
        fetch: async () => new Response(body, { status: 200 }),
        url: '/big',
        headers: {},
        store: sink,
        onProgress: (done) => {
          // A cada 8 MiB: coleta e mede o que continua RETIDO (o downloader não pode acumular o arquivo).
          if (done - lastSample >= 8 * CHUNK) {
            lastSample = done;
            gc!();
            peak = Math.max(peak, process.memoryUsage().arrayBuffers - base);
          }
        },
      },
    );
    expect(r.bytesFetched).toBe(SIZE);
    expect(written).toBe(SIZE);
    expect(committed).toBe(true);
    // Memória retida (após GC) muito abaixo do tamanho do arquivo (128 MiB): só alguns blocos de 1 MiB em trânsito (< 1/8).
    expect(peak).toBeLessThan(16 * 1024 * 1024);
  }, 120_000);
});

describe('DownloadQueue — concorrência, backoff com jitter e falha permanente', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  const jobs = (n: number): DownloadJob[] =>
    Array.from({ length: n }, (_, i) => ({ key: `m${i}`, media_id: `id${i}`, sha256: `${i}`.repeat(64).slice(0, 64), size: 100, mime: 'image/png' }));

  it('no máximo 2 downloads simultâneos por padrão (limite 3)', async () => {
    let running = 0;
    let max = 0;
    const q = new DownloadQueue({
      executor: async () => {
        running++;
        max = Math.max(max, running);
        await new Promise((r) => setTimeout(r, 5));
        running--;
      },
    });
    const r = await q.run(jobs(7), new AbortController().signal);
    expect(r.completed).toHaveLength(7);
    expect(max).toBe(2);
    expect(new DownloadQueue({ executor: async () => undefined, concurrency: 10 }).concurrency).toBe(3);
  });

  it('falha permanente após 5 tentativas em ≤ 30 min; as demais concluem', async () => {
    vi.useFakeTimers();
    const t0 = Date.now();
    const attemptsAt: number[] = [];
    const failures: [number, boolean][] = [];
    const q = new DownloadQueue({
      now: () => Date.now(),
      random: () => 0.5,
      executor: async (j) => {
        if (j.key === 'm1') {
          attemptsAt.push(Date.now() - t0);
          throw new DownloadError('HTTP', 'HTTP 500', 500);
        }
      },
      onAttemptFailed: (_j, _e, attempts, permanent) => failures.push([attempts, permanent]),
    });
    const p = q.run(jobs(3), new AbortController().signal);
    await vi.advanceTimersByTimeAsync(31 * 60_000);
    const r = await p;
    expect(r.completed.sort()).toEqual(['m0', 'm2']);
    expect(r.failed).toHaveLength(1);
    expect(r.failed[0]!.attempts).toBe(5);
    expect(failures.at(-1)).toEqual([5, true]);
    expect(attemptsAt).toHaveLength(5);
    expect(attemptsAt.at(-1)!).toBeLessThanOrEqual(30 * 60_000);
    // atrasos base 5 s, 30 s, 2 min, 8 min (jitter 0,5 ⇒ fator 1,0)
    const gaps = attemptsAt.slice(1).map((t, i) => t - attemptsAt[i]!);
    expect(gaps).toEqual([5_000, 30_000, 120_000, 480_000]);
  });

  it('jitter: atrasos ficam em [0,8; 1,2] × base', async () => {
    vi.useFakeTimers();
    for (const rnd of [0, 0.999]) {
      const at: number[] = [];
      const q = new DownloadQueue({
        now: () => Date.now(),
        random: () => rnd,
        executor: async () => {
          at.push(Date.now());
          throw new DownloadError('SHA_MISMATCH', 'x');
        },
      });
      const p = q.run(jobs(1), new AbortController().signal);
      await vi.advanceTimersByTimeAsync(40 * 60_000);
      await p;
      const gap = at[1]! - at[0]!;
      expect(gap).toBeGreaterThanOrEqual(4_000);
      expect(gap).toBeLessThanOrEqual(6_000);
    }
  });

  it('erros de rede não contam como tentativa da mídia (conectividade ≠ mídia)', async () => {
    vi.useFakeTimers();
    let n = 0;
    const q = new DownloadQueue({
      now: () => Date.now(),
      executor: async () => {
        if (n++ < 12) throw new DownloadError('NETWORK', 'offline');
      },
    });
    const p = q.run(jobs(1), new AbortController().signal);
    await vi.advanceTimersByTimeAsync(60 * 60_000);
    const r = await p;
    expect(r.completed).toEqual(['m0']);
    expect(r.failed).toHaveLength(0);
  });

  it('QUOTA é fatal: interrompe a fila', async () => {
    const q = new DownloadQueue({
      executor: async (j) => {
        if (j.key === 'm0') throw new DownloadError('QUOTA', 'QuotaExceededError');
        await new Promise((r) => setTimeout(r, 20));
      },
    });
    const r = await q.run(jobs(4), new AbortController().signal);
    expect(r.fatal?.kind).toBe('QUOTA');
  });

  it('abort (manifesto mais novo) encerra a fila', async () => {
    const ctrl = new AbortController();
    const q = new DownloadQueue({
      executor: (_j, { signal }) =>
        new Promise((_res, rej) => {
          signal.addEventListener('abort', () => rej(Object.assign(new Error('aborted'), { name: 'AbortError' })));
        }),
    });
    const p = q.run(jobs(3), ctrl.signal);
    ctrl.abort();
    const r = await p;
    expect(r.aborted).toBe(true);
    expect(r.completed).toHaveLength(0);
  });
});
