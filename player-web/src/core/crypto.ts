/**
 * ES256 (ECDSA P-256 + SHA-256) do RDPP §0/§1.
 * - Assinatura no formato BRUTO r‖s (64 bytes), base64url sem padding — é o formato nativo do WebCrypto.
 * - Chave pública = SPKI DER em base64url sem padding.
 * - Conversões DER⇄raw para interoperar com bibliotecas que emitem DER (OpenSSL, `cryptography`, Node).
 * - SHA-256 incremental via @noble/hashes (JS puro: sem WASM, compatível com CSP estrita).
 *
 * Sem DOM: usa apenas `globalThis.crypto.subtle` (presente em navegadores em contexto seguro e no Node ≥ 20).
 */

import { sha256 } from '@noble/hashes/sha2';
import { canonicalBytes } from './canonical';
import { b64uDecode, b64uEncode, hexEncode } from './util';

export const ES256_KEY_PARAMS: EcKeyGenParams = { name: 'ECDSA', namedCurve: 'P-256' };
export const ES256_SIGN_PARAMS: EcdsaParams = { name: 'ECDSA', hash: 'SHA-256' };

export interface Signer {
  /** Assina bytes; devolve r‖s (64 bytes). */
  sign(data: Uint8Array): Promise<Uint8Array>;
  /** Chave pública SPKI DER. */
  publicKeySpki(): Promise<Uint8Array>;
}

export interface Verifier {
  verify(data: Uint8Array, signature: Uint8Array): Promise<boolean>;
}

export function getSubtle(): SubtleCrypto {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) {
    throw new Error('WebCrypto indisponível: o player exige contexto seguro (HTTPS ou localhost).');
  }
  return subtle;
}

/** Copia para um ArrayBuffer próprio (evita SharedArrayBuffer/offsets em APIs WebCrypto). */
function toBuffer(bytes: Uint8Array): ArrayBuffer {
  const copy = new Uint8Array(bytes.length);
  copy.set(bytes);
  return copy.buffer;
}

/** Gera o par do dispositivo. Em produção `extractable=false` (a privada nunca sai do WebCrypto). */
export async function generateEs256KeyPair(extractable = false, subtle: SubtleCrypto = getSubtle()): Promise<CryptoKeyPair> {
  return subtle.generateKey(ES256_KEY_PARAMS, extractable, ['sign', 'verify']);
}

export class WebCryptoSigner implements Signer {
  constructor(
    private readonly keyPair: CryptoKeyPair,
    private readonly subtle: SubtleCrypto = getSubtle(),
  ) {}

  async sign(data: Uint8Array): Promise<Uint8Array> {
    const sig = await this.subtle.sign(ES256_SIGN_PARAMS, this.keyPair.privateKey, toBuffer(data));
    return new Uint8Array(sig);
  }

  async publicKeySpki(): Promise<Uint8Array> {
    // A chave pública é sempre exportável, mesmo com o par gerado como não exportável.
    return new Uint8Array(await this.subtle.exportKey('spki', this.keyPair.publicKey));
  }

  get privateKeyExtractable(): boolean {
    return this.keyPair.privateKey.extractable;
  }
}

export class WebCryptoVerifier implements Verifier {
  constructor(
    private readonly key: CryptoKey,
    private readonly subtle: SubtleCrypto = getSubtle(),
  ) {}

  async verify(data: Uint8Array, signature: Uint8Array): Promise<boolean> {
    if (signature.length !== 64) return false;
    try {
      return await this.subtle.verify(ES256_SIGN_PARAMS, this.key, toBuffer(signature), toBuffer(data));
    } catch {
      return false;
    }
  }
}

export async function importSpkiVerifier(spki: Uint8Array | string, subtle: SubtleCrypto = getSubtle()): Promise<Verifier> {
  const der = typeof spki === 'string' ? b64uDecode(spki) : spki;
  const key = await subtle.importKey('spki', toBuffer(der), ES256_KEY_PARAMS, true, ['verify']);
  return new WebCryptoVerifier(key, subtle);
}

/** Importa uma privada PKCS#8 (apenas testes/vetores — nunca usado para a chave do dispositivo). */
export async function importPkcs8Signer(
  pkcs8: Uint8Array | string,
  spki: Uint8Array | string,
  subtle: SubtleCrypto = getSubtle(),
): Promise<Signer> {
  const priv = await subtle.importKey(
    'pkcs8',
    toBuffer(typeof pkcs8 === 'string' ? b64uDecode(pkcs8) : pkcs8),
    ES256_KEY_PARAMS,
    false,
    ['sign'],
  );
  const pub = await subtle.importKey(
    'spki',
    toBuffer(typeof spki === 'string' ? b64uDecode(spki) : spki),
    ES256_KEY_PARAMS,
    true,
    ['verify'],
  );
  return new WebCryptoSigner({ privateKey: priv, publicKey: pub }, subtle);
}

/** Assina o RDJ1 de um objeto; devolve base64url do r‖s. */
export async function signCanonical(signer: Signer, value: unknown): Promise<string> {
  return b64uEncode(await signer.sign(canonicalBytes(value)));
}

/** Verifica assinatura base64url (r‖s) sobre o RDJ1 do objeto. Assinatura malformada ⇒ false. */
export async function verifyCanonical(verifier: Verifier, value: unknown, signatureB64u: string): Promise<boolean> {
  let sig: Uint8Array;
  try {
    sig = b64uDecode(signatureB64u);
  } catch {
    return false;
  }
  return verifier.verify(canonicalBytes(value), sig);
}

// ───────────────────────── DER ⇄ raw (r‖s) ─────────────────────────

function readDerLength(der: Uint8Array, pos: number): [len: number, next: number] {
  const first = der[pos];
  if (first === undefined) throw new Error('DER truncado');
  if (first < 0x80) return [first, pos + 1];
  const n = first & 0x7f;
  if (n === 0 || n > 2) throw new Error('DER: comprimento não suportado');
  let len = 0;
  for (let i = 0; i < n; i++) {
    const b = der[pos + 1 + i];
    if (b === undefined) throw new Error('DER truncado');
    len = (len << 8) | b;
  }
  return [len, pos + 1 + n];
}

function readDerInteger(der: Uint8Array, pos: number): [value: Uint8Array, next: number] {
  if (der[pos] !== 0x02) throw new Error('DER: INTEGER esperado');
  const [len, start] = readDerLength(der, pos + 1);
  const end = start + len;
  if (end > der.length || len === 0) throw new Error('DER truncado');
  let v = der.subarray(start, end);
  while (v.length > 1 && v[0] === 0) v = v.subarray(1);
  return [v, end];
}

/** ECDSA-Sig-Value DER → r‖s com `size` bytes por componente (32 para P-256). */
export function derToRaw(der: Uint8Array, size = 32): Uint8Array {
  if (der[0] !== 0x30) throw new Error('DER: SEQUENCE esperada');
  const [seqLen, body] = readDerLength(der, 1);
  if (body + seqLen !== der.length) throw new Error('DER: comprimento da SEQUENCE inconsistente');
  const [r, afterR] = readDerInteger(der, body);
  const [s, afterS] = readDerInteger(der, afterR);
  if (afterS !== der.length) throw new Error('DER: bytes extras');
  if (r.length > size || s.length > size) throw new Error('DER: inteiro maior que a curva');
  const out = new Uint8Array(size * 2);
  out.set(r, size - r.length);
  out.set(s, size * 2 - s.length);
  return out;
}

function derInteger(v: Uint8Array): Uint8Array {
  let i = 0;
  while (i < v.length - 1 && v[i] === 0) i++;
  let body = v.subarray(i);
  if (body[0]! & 0x80) {
    const padded = new Uint8Array(body.length + 1);
    padded.set(body, 1);
    body = padded;
  }
  const out = new Uint8Array(2 + body.length);
  out[0] = 0x02;
  out[1] = body.length;
  out.set(body, 2);
  return out;
}

/** r‖s → ECDSA-Sig-Value DER. */
export function rawToDer(raw: Uint8Array): Uint8Array {
  if (raw.length % 2 !== 0) throw new Error('assinatura raw com tamanho ímpar');
  const half = raw.length / 2;
  const r = derInteger(raw.subarray(0, half));
  const s = derInteger(raw.subarray(half));
  const len = r.length + s.length;
  if (len > 127) throw new Error('assinatura grande demais');
  const out = new Uint8Array(2 + len);
  out[0] = 0x30;
  out[1] = len;
  out.set(r, 2);
  out.set(s, 2 + r.length);
  return out;
}

// ───────────────────────── SHA-256 ─────────────────────────

export interface IncrementalHash {
  update(chunk: Uint8Array): void;
  digestHex(): string;
}

/** SHA-256 incremental: estado de 32 bytes, nunca acumula o arquivo em memória. */
export function createSha256(): IncrementalHash {
  const h = sha256.create();
  return {
    update(chunk: Uint8Array) {
      h.update(chunk);
    },
    digestHex() {
      return hexEncode(h.digest());
    },
  };
}

export function sha256Hex(data: Uint8Array): string {
  return hexEncode(sha256(data));
}

/** sha256 hex do RDJ1 do valor (usado no prev_hash do PoP). */
export function canonicalSha256Hex(value: unknown): string {
  return sha256Hex(canonicalBytes(value));
}
