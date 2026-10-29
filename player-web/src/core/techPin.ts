/**
 * PIN técnico (RDPP §4 `set_tech_pin`): o servidor envia SÓ `{algo:"pbkdf2-sha256", iterations, salt_b64u, hash_b64u}`
 * (nunca o PIN em claro). O player guarda o registro no StateStore (IndexedDB) e verifica o PIN digitado com
 * PBKDF2-HMAC-SHA256 via WebCrypto (`deriveBits`), comparação em tempo constante. Usado pelo menu técnico/saída do
 * kiosk das cascas (no navegador, protege o formulário de login controlado do técnico).
 */

import { getSubtle } from './crypto';
import { ProtocolError } from './protocol';
import { STATE_KEYS, type StateStore } from './storage';
import { b64uDecode, bytesEqual, utf8 } from './util';

export interface TechPinRecord {
  algo: 'pbkdf2-sha256';
  iterations: number;
  salt_b64u: string;
  hash_b64u: string;
  set_at_ms: number;
}

const MIN_ITER = 100_000;
const MAX_ITER = 5_000_000;

/** Valida os argumentos do comando (sem aceitar PIN em claro). */
export function parseTechPinArgs(args: Record<string, unknown> | null, nowMs: number): TechPinRecord {
  if (!args) throw new ProtocolError('BAD_ARGS', 'set_tech_pin sem args');
  if ('pin' in args) throw new ProtocolError('BAD_ARGS', 'PIN em claro não é aceito');
  const { algo, iterations, salt_b64u, hash_b64u } = args;
  if (algo !== 'pbkdf2-sha256') throw new ProtocolError('BAD_ARGS', `algoritmo não suportado: ${String(algo)}`);
  if (typeof iterations !== 'number' || !Number.isSafeInteger(iterations) || iterations < MIN_ITER || iterations > MAX_ITER) {
    throw new ProtocolError('BAD_ARGS', 'iterations fora do intervalo');
  }
  if (typeof salt_b64u !== 'string' || typeof hash_b64u !== 'string') throw new ProtocolError('BAD_ARGS', 'salt/hash ausentes');
  const salt = b64uDecode(salt_b64u);
  const hash = b64uDecode(hash_b64u);
  if (salt.length < 16 || hash.length !== 32) throw new ProtocolError('BAD_ARGS', 'tamanho de salt/hash inválido');
  return { algo, iterations, salt_b64u, hash_b64u, set_at_ms: nowMs };
}

export async function pbkdf2Sha256(pin: string, salt: Uint8Array, iterations: number, subtle: SubtleCrypto = getSubtle()): Promise<Uint8Array> {
  const key = await subtle.importKey('raw', utf8(pin).slice(), 'PBKDF2', false, ['deriveBits']);
  const bits = await subtle.deriveBits({ name: 'PBKDF2', hash: 'SHA-256', salt: salt.slice(), iterations }, key, 256);
  return new Uint8Array(bits);
}

export class TechPinStore {
  constructor(private readonly store: StateStore) {}

  async set(args: Record<string, unknown> | null, nowMs: number): Promise<TechPinRecord> {
    const rec = parseTechPinArgs(args, nowMs);
    await this.store.set(STATE_KEYS.techPin, rec);
    return rec;
  }

  async get(): Promise<TechPinRecord | null> {
    return (await this.store.get<TechPinRecord>(STATE_KEYS.techPin)) ?? null;
  }

  /** null = nenhum PIN configurado. */
  async verify(pin: string): Promise<boolean | null> {
    const rec = await this.get();
    if (!rec) return null;
    const got = await pbkdf2Sha256(pin, b64uDecode(rec.salt_b64u), rec.iterations);
    return bytesEqual(got, b64uDecode(rec.hash_b64u));
  }
}
