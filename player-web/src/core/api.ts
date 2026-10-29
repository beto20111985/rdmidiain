/**
 * Cliente HTTP de /api/v1/player/* (sem DOM; usa `fetch` injetável).
 *  - Mede o offset do relógio por `X-Server-Time-Ms` (RDPP §0) com RTT monotônico.
 *  - Bearer opcional; 401 com token expirado ⇒ invalida e tenta 1× de novo; 401 `DEVICE_REVOKED` ⇒ callback.
 *  - Envelope de erro `{"error":{code,message,details}}` vira `ApiError`.
 *  - Nunca coloca segredo/token em URL.
 */

import type { Clock } from './clock';
import { HEADER_SERVER_TIME, parseApiError } from './protocol';
import { realMono, type MonoClock } from './util';

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly details: unknown = null,
  ) {
    super(`${status} ${code}: ${message}`);
    this.name = 'ApiError';
  }
  get isNetwork(): boolean {
    return this.status === 0;
  }
  get isRevoked(): boolean {
    return this.code === 'DEVICE_REVOKED';
  }
  /** Erros que valem nova tentativa com backoff. */
  get retryable(): boolean {
    return this.status === 0 || this.status === 408 || this.status === 425 || this.status === 429 || this.status >= 500;
  }
}

export interface TokenSource {
  token(): Promise<string>;
  invalidate(): void;
}

export interface ApiClientOptions {
  baseUrl: string;
  fetch?: FetchLike;
  clock?: Clock;
  mono?: MonoClock;
  auth?: TokenSource;
  onRevoked?: () => void;
  timeoutMs?: number;
}

export interface RequestOptions {
  body?: unknown;
  auth?: boolean;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  /** Status não-2xx aceitos sem exceção (ex.: 304, 204). */
  accept?: number[];
}

export interface ApiResponse<T> {
  status: number;
  data: T | null;
  headers: Headers;
}

export class ApiClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: FetchLike;
  private readonly mono: MonoClock;
  private readonly timeoutMs: number;
  auth: TokenSource | undefined;
  onRevoked: (() => void) | undefined;

  constructor(private readonly opts: ApiClientOptions) {
    this.baseUrl = opts.baseUrl.replace(/\/+$/, '');
    this.fetchImpl = opts.fetch ?? ((input, init) => globalThis.fetch(input, init));
    this.mono = opts.mono ?? realMono;
    this.timeoutMs = opts.timeoutMs ?? 30_000;
    this.auth = opts.auth;
    this.onRevoked = opts.onRevoked;
  }

  url(path: string): string {
    return this.baseUrl + path;
  }

  get<T>(path: string, o: RequestOptions = {}): Promise<ApiResponse<T>> {
    return this.request<T>('GET', path, o);
  }

  post<T>(path: string, body: unknown, o: RequestOptions = {}): Promise<ApiResponse<T>> {
    return this.request<T>('POST', path, { ...o, body });
  }

  async request<T>(method: string, path: string, o: RequestOptions = {}): Promise<ApiResponse<T>> {
    const useAuth = o.auth ?? true;
    for (let attempt = 0; ; attempt++) {
      const headers: Record<string, string> = { Accept: 'application/json', ...(o.headers ?? {}) };
      if (o.body !== undefined) headers['Content-Type'] = 'application/json';
      if (useAuth) {
        if (!this.auth) throw new ApiError(0, 'NO_AUTH', 'cliente sem fonte de token');
        headers.Authorization = `Bearer ${await this.auth.token()}`;
      }
      const res = await this.send(method, path, headers, o);
      if (res.status === 401) {
        const err = await readError(res);
        if (err.code === 'DEVICE_REVOKED') {
          this.onRevoked?.();
          throw err;
        }
        if (useAuth && attempt === 0) {
          this.auth?.invalidate();
          continue;
        }
        throw err;
      }
      if (res.ok || (o.accept ?? []).includes(res.status)) {
        return { status: res.status, data: await readJson<T>(res), headers: res.headers };
      }
      throw await readError(res);
    }
  }

  /** Requisição crua (download de mídia): devolve o Response sem consumir o corpo. */
  async raw(path: string, init: { headers?: Record<string, string>; signal?: AbortSignal; auth?: boolean } = {}): Promise<Response> {
    const headers: Record<string, string> = { ...(init.headers ?? {}) };
    if (init.auth ?? true) {
      if (!this.auth) throw new ApiError(0, 'NO_AUTH', 'cliente sem fonte de token');
      headers.Authorization = `Bearer ${await this.auth.token()}`;
    }
    return this.send('GET', path, headers, { signal: init.signal }, false);
  }

  private async send(
    method: string,
    path: string,
    headers: Record<string, string>,
    o: { body?: unknown; signal?: AbortSignal | undefined },
    withTimeout = true,
  ): Promise<Response> {
    const ctrl = new AbortController();
    const onAbort = (): void => ctrl.abort();
    o.signal?.addEventListener('abort', onAbort, { once: true });
    const timer = withTimeout ? setTimeout(() => ctrl.abort(), this.timeoutMs) : undefined;
    const sent = this.mono();
    try {
      const res = await this.fetchImpl(this.url(path), {
        method,
        headers,
        body: o.body === undefined ? undefined : JSON.stringify(o.body),
        signal: ctrl.signal,
        cache: 'no-store',
        credentials: 'omit',
        referrerPolicy: 'no-referrer',
      });
      const serverTime = Number(res.headers.get(HEADER_SERVER_TIME));
      if (this.opts.clock && Number.isFinite(serverTime) && serverTime > 0) {
        this.opts.clock.observe(serverTime, sent, this.mono());
      }
      return res;
    } catch (e) {
      if (o.signal?.aborted) throw e;
      throw new ApiError(0, 'NETWORK', e instanceof Error ? e.message : String(e));
    } finally {
      if (timer !== undefined) clearTimeout(timer);
      o.signal?.removeEventListener('abort', onAbort);
    }
  }
}

async function readJson<T>(res: Response): Promise<T | null> {
  if (res.status === 204 || res.status === 304) return null;
  const text = await res.text();
  if (!text) return null;
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new ApiError(res.status, 'BAD_JSON', 'resposta não é JSON');
  }
}

async function readError(res: Response): Promise<ApiError> {
  let body: unknown = null;
  try {
    const text = await res.text();
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }
  const e = parseApiError(body);
  return new ApiError(res.status, e?.code ?? `HTTP_${res.status}`, e?.message ?? res.statusText, e?.details ?? null);
}
