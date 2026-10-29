import { createHash } from 'node:crypto';

export function bytesOf(size: number, seed = 1): Uint8Array {
  const out = new Uint8Array(size);
  let x = seed >>> 0 || 1;
  for (let i = 0; i < size; i++) {
    x ^= x << 13;
    x ^= x >>> 17;
    x ^= x << 5;
    out[i] = x & 0xff;
  }
  return out;
}

export function shaHex(data: Uint8Array): string {
  return createHash('sha256').update(data).digest('hex');
}

export interface ServedRequest {
  url: string;
  headers: Record<string, string>;
  status: number;
}

export interface FakeMediaServerOptions {
  /** Corta a conexão (erro na leitura do corpo) após N bytes enviados nesta resposta. */
  cutAfter?: (requestIndex: number) => number | null;
  /** Ignora Range e responde 200 com o arquivo inteiro. */
  ignoreRange?: boolean;
  /** Status forçado por requisição (ex.: 500). */
  statusFor?: (requestIndex: number) => number | null;
  chunk?: number;
  /** Segura o envio do corpo até a promessa resolver (para testar cancelamento). */
  gate?: Promise<void>;
}

/** Servidor HTTP falso com Range/If-Range (ETag = "<sha256>"), como o RDPP §6. */
export function fakeMediaServer(files: Map<string, Uint8Array>, opts: FakeMediaServerOptions = {}) {
  const requests: ServedRequest[] = [];
  const fetch = async (url: string, init: RequestInit = {}): Promise<Response> => {
    const idx = requests.length;
    const headers = Object.fromEntries(Object.entries((init.headers ?? {}) as Record<string, string>).map(([k, v]) => [k.toLowerCase(), v]));
    const id = url.split('/').pop()!;
    const data = files.get(id);
    const forced = opts.statusFor?.(idx) ?? null;
    if (forced) {
      requests.push({ url, headers, status: forced });
      return new Response(JSON.stringify({ error: { code: 'X', message: 'forced' } }), { status: forced });
    }
    if (!data) {
      requests.push({ url, headers, status: 404 });
      return new Response('', { status: 404 });
    }
    const etag = `"${shaHex(data)}"`;
    let start = 0;
    let status = 200;
    const range = headers.range;
    if (range && !opts.ignoreRange && (!headers['if-range'] || headers['if-range'] === etag)) {
      const m = /^bytes=(\d+)-$/.exec(range);
      if (m) {
        start = Number(m[1]);
        if (start >= data.length) {
          requests.push({ url, headers, status: 416 });
          return new Response('', { status: 416 });
        }
        status = 206;
      }
    }
    const cut = opts.cutAfter?.(idx) ?? null;
    const chunk = opts.chunk ?? 64 * 1024;
    let pos = start;
    let sent = 0;
    const body = new ReadableStream<Uint8Array>({
      async pull(ctrl) {
        if (opts.gate) await opts.gate;
        if (init.signal?.aborted) {
          ctrl.error(Object.assign(new Error('aborted'), { name: 'AbortError' }));
          return;
        }
        if (cut !== null && sent >= cut) {
          ctrl.error(new Error('conexão encerrada'));
          return;
        }
        if (pos >= data.length) {
          ctrl.close();
          return;
        }
        let n = Math.min(chunk, data.length - pos);
        if (cut !== null) n = Math.min(n, cut - sent);
        ctrl.enqueue(data.slice(pos, pos + n));
        pos += n;
        sent += n;
      },
    });
    const h = new Headers({ ETag: etag, 'Content-Type': 'application/octet-stream' });
    if (status === 206) h.set('Content-Range', `bytes ${start}-${data.length - 1}/${data.length}`);
    requests.push({ url, headers, status });
    return new Response(body, { status, headers: h });
  };
  return { fetch, requests };
}
