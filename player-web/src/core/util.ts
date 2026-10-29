/**
 * Utilitários do núcleo (sem DOM): base64url, hex, UTF-8, aleatoriedade criptográfica,
 * UUID v4, backoff com jitter, mutex e relógios injetáveis.
 */

const B64U = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';
const B64U_LOOKUP: Int16Array = (() => {
  const t = new Int16Array(128).fill(-1);
  for (let i = 0; i < B64U.length; i++) t[B64U.charCodeAt(i)] = i;
  return t;
})();

/** base64url sem padding (RFC 4648 §5). */
export function b64uEncode(bytes: Uint8Array): string {
  let out = '';
  let i = 0;
  for (; i + 2 < bytes.length; i += 3) {
    const n = (bytes[i]! << 16) | (bytes[i + 1]! << 8) | bytes[i + 2]!;
    out += B64U[(n >> 18) & 63]! + B64U[(n >> 12) & 63]! + B64U[(n >> 6) & 63]! + B64U[n & 63]!;
  }
  const rest = bytes.length - i;
  if (rest === 1) {
    const n = bytes[i]! << 16;
    out += B64U[(n >> 18) & 63]! + B64U[(n >> 12) & 63]!;
  } else if (rest === 2) {
    const n = (bytes[i]! << 16) | (bytes[i + 1]! << 8);
    out += B64U[(n >> 18) & 63]! + B64U[(n >> 12) & 63]! + B64U[(n >> 6) & 63]!;
  }
  return out;
}

/** Decodifica base64url sem padding; rejeita caracteres inválidos, padding e comprimento impossível. */
export function b64uDecode(text: string): Uint8Array {
  if (text.length % 4 === 1) throw new Error('base64url: comprimento inválido');
  const out = new Uint8Array(Math.floor((text.length * 3) / 4));
  let buf = 0;
  let bits = 0;
  let o = 0;
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    const v = c < 128 ? B64U_LOOKUP[c]! : -1;
    if (v < 0) throw new Error('base64url: caractere inválido');
    buf = (buf << 6) | v;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      out[o++] = (buf >> bits) & 0xff;
    }
  }
  // bits residuais devem ser zero (forma canônica)
  if (bits > 0 && (buf & ((1 << bits) - 1)) !== 0) throw new Error('base64url: bits residuais não nulos');
  return out.subarray(0, o);
}

export function hexEncode(bytes: Uint8Array): string {
  let s = '';
  for (let i = 0; i < bytes.length; i++) s += bytes[i]!.toString(16).padStart(2, '0');
  return s;
}

export function hexDecode(hex: string): Uint8Array {
  if (hex.length % 2 !== 0 || !/^[0-9a-fA-F]*$/.test(hex)) throw new Error('hex inválido');
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return out;
}

const encoder = new TextEncoder();
export function utf8(text: string): Uint8Array {
  return encoder.encode(text);
}

export function bytesEqual(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i]! ^ b[i]!;
  return diff === 0;
}

export function concatBytes(...parts: Uint8Array[]): Uint8Array {
  const len = parts.reduce((n, p) => n + p.length, 0);
  const out = new Uint8Array(len);
  let o = 0;
  for (const p of parts) {
    out.set(p, o);
    o += p.length;
  }
  return out;
}

/** Fonte de aleatoriedade criptográfica (getRandomValues funciona também fora de contexto seguro). */
export type RandomBytes = (n: number) => Uint8Array;

export const cryptoRandomBytes: RandomBytes = (n) => {
  const out = new Uint8Array(n);
  globalThis.crypto.getRandomValues(out);
  return out;
};

/** UUID v4 minúsculo a partir de bytes aleatórios (não depende de crypto.randomUUID/contexto seguro). */
export function uuidv4(random: RandomBytes = cryptoRandomBytes): string {
  const b = random(16);
  b[6] = (b[6]! & 0x0f) | 0x40;
  b[8] = (b[8]! & 0x3f) | 0x80;
  const h = hexEncode(b);
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

/**
 * Backoff exponencial com "equal jitter": metade fixa + metade aleatória.
 * attempt 0 → [base/2, base]; cresce 2× por tentativa até `maxMs`.
 */
export function backoffDelay(attempt: number, baseMs: number, maxMs: number, random: () => number = Math.random): number {
  const exp = Math.min(maxMs, baseMs * Math.pow(2, Math.max(0, attempt)));
  return Math.floor(exp / 2 + random() * (exp / 2));
}

/** Matemática inteira: módulo sempre não negativo. */
export function mod(a: number, n: number): number {
  return ((a % n) + n) % n;
}

/** Serializa execuções assíncronas (seq/prev_hash do PoP, commit de manifesto). */
export class Mutex {
  private tail: Promise<unknown> = Promise.resolve();
  run<T>(fn: () => Promise<T>): Promise<T> {
    const result = this.tail.then(fn, fn);
    this.tail = result.catch(() => undefined);
    return result;
  }
}

export interface Timers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export const realTimers: Timers = {
  setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
  clearTimeout: (h) => globalThis.clearTimeout(h as ReturnType<typeof setTimeout>),
};

export function sleep(ms: number, timers: Timers = realTimers, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(abortError());
      return;
    }
    const h = timers.setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    const onAbort = (): void => {
      timers.clearTimeout(h);
      reject(abortError());
    };
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

export function abortError(): Error {
  const e = new Error('aborted');
  e.name = 'AbortError';
  return e;
}

export function isAbortError(e: unknown): boolean {
  return e instanceof Error && e.name === 'AbortError';
}

/** Relógio monotônico (ms, fracionário). */
export type MonoClock = () => number;
export const realMono: MonoClock = () => globalThis.performance.now();

export function errorMessage(e: unknown): string {
  if (e instanceof Error) return e.message;
  return String(e);
}

export const DAY_MS = 86_400_000;
