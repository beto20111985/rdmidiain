/**
 * Interfaces de armazenamento do núcleo. Implementações:
 *  - web: OPFS (mídia) / IndexedDB (estado, outbox, chave) — `src/adapters/web/*`;
 *  - testes e degradação: memória — `memoryStores.ts`.
 * `localStorage` é proibido para mídia, fila e credenciais (ADR_008).
 */

import type { Signer } from './crypto';
import type { PopEvent } from './protocol';

/** Escritor sequencial de um download parcial. */
export interface MediaWriter {
  write(chunk: Uint8Array): Promise<void>;
  close(): Promise<void>;
}

/** Mídia endereçada por sha256 (hex minúsculo). Só arquivos com hash verificado são "committed". */
export interface MediaStore {
  readonly kind: string;
  /** Bytes já gravados do parcial (0 se não houver). */
  partialSize(sha256: string): Promise<number>;
  /** Abre escrita no parcial a partir de `offset` (trunca o que estiver além). */
  openWriter(sha256: string, offset: number): Promise<MediaWriter>;
  /** Lê o parcial em blocos (para reconstruir o hash incremental na retomada). */
  readPartial(sha256: string): AsyncIterable<Uint8Array>;
  discardPartial(sha256: string): Promise<void>;
  /** Promove o parcial (já verificado) a arquivo definitivo. */
  commit(sha256: string): Promise<void>;
  has(sha256: string): Promise<boolean>;
  /** sha256 dos arquivos definitivos. */
  list(): Promise<string[]>;
  remove(sha256: string): Promise<void>;
  /** URL reproduzível (blob:) sem carregar o arquivo inteiro em memória quando possível. */
  getUrl(sha256: string): Promise<string>;
  releaseUrl(url: string): void;
}

export interface OutboxRecord {
  /** Chave do outbox: contador global do aparelho (nunca reinicia, nem em re-pareamento). */
  oid: number;
  seq: number;
  playback_uuid: string;
  event: PopEvent;
  signature: string;
}

export interface StateTransaction {
  set?: Record<string, unknown>;
  del?: string[];
  outboxPut?: OutboxRecord[];
  /** oids a remover. */
  outboxDelete?: number[];
}

/** Estado chave-valor + outbox de PoP, com transações atômicas (commit de manifesto, seq/prev_hash). */
export interface StateStore {
  get<T>(key: string): Promise<T | undefined>;
  set(key: string, value: unknown): Promise<void>;
  delete(key: string): Promise<void>;
  transact(tx: StateTransaction): Promise<void>;
  /** Eventos pendentes em ordem de gravação (oid crescente). */
  outboxList(limit: number): Promise<OutboxRecord[]>;
  outboxCount(): Promise<number>;
  outboxClear(): Promise<void>;
}

/** Par ES256 do dispositivo (não exportável). */
export interface KeyStore {
  load(): Promise<Signer | null>;
  create(): Promise<Signer>;
  clear(): Promise<void>;
}

/** Chaves usadas no StateStore. */
export const STATE_KEYS = {
  device: 'device',
  manifestActive: 'manifest.active',
  manifestStaged: 'manifest.staged',
  manifestPrevious: 'manifest.previous',
  anchor: 'anchor',
  popChain: 'pop.chain',
  popOid: 'pop.oid',
  kills: 'kills',
  commands: 'commands',
  syncInfo: 'sync.info',
  clock: 'clock',
  mediaFailures: 'media.failures',
  techPin: 'tech.pin',
} as const;
