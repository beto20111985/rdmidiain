/**
 * Implementações em memória das interfaces de armazenamento.
 * Usadas nos testes (Node) e como degradação quando IndexedDB/OPFS não existem (nada persiste — o app alerta).
 */

import { generateEs256KeyPair, WebCryptoSigner, type Signer } from './crypto';
import type { KeyStore, MediaStore, MediaWriter, OutboxRecord, StateStore, StateTransaction } from './storage';
import { concatBytes } from './util';

function clone<T>(v: T): T {
  return v === undefined ? v : (structuredClone(v) as T);
}

export class MemoryStateStore implements StateStore {
  readonly kv = new Map<string, unknown>();
  readonly outbox = new Map<number, OutboxRecord>();
  /** Falha injetável para testes de atomicidade. */
  failNextTransact: Error | null = null;

  async get<T>(key: string): Promise<T | undefined> {
    return clone(this.kv.get(key) as T | undefined);
  }
  async set(key: string, value: unknown): Promise<void> {
    this.kv.set(key, clone(value));
  }
  async delete(key: string): Promise<void> {
    this.kv.delete(key);
  }
  async transact(tx: StateTransaction): Promise<void> {
    if (this.failNextTransact) {
      const e = this.failNextTransact;
      this.failNextTransact = null;
      throw e;
    }
    for (const [k, v] of Object.entries(tx.set ?? {})) this.kv.set(k, clone(v));
    for (const k of tx.del ?? []) this.kv.delete(k);
    for (const r of tx.outboxPut ?? []) this.outbox.set(r.oid, clone(r));
    for (const id of tx.outboxDelete ?? []) this.outbox.delete(id);
  }
  async outboxList(limit: number): Promise<OutboxRecord[]> {
    return [...this.outbox.values()]
      .sort((a, b) => a.oid - b.oid)
      .slice(0, limit)
      .map(clone);
  }
  async outboxCount(): Promise<number> {
    return this.outbox.size;
  }
  async outboxClear(): Promise<void> {
    this.outbox.clear();
  }
}

export class MemoryMediaStore implements MediaStore {
  readonly kind = 'memory';
  readonly partials = new Map<string, Uint8Array[]>();
  readonly files = new Map<string, Uint8Array>();
  private urls = 0;

  async partialSize(sha: string): Promise<number> {
    return (this.partials.get(sha) ?? []).reduce((n, c) => n + c.length, 0);
  }
  async openWriter(sha: string, offset: number): Promise<MediaWriter> {
    const existing = concatBytes(...(this.partials.get(sha) ?? []));
    const kept = existing.subarray(0, Math.min(offset, existing.length));
    const chunks: Uint8Array[] = kept.length ? [kept.slice()] : [];
    this.partials.set(sha, chunks);
    return {
      write: async (chunk) => {
        chunks.push(chunk.slice());
      },
      close: async () => undefined,
    };
  }
  async *readPartial(sha: string): AsyncIterable<Uint8Array> {
    for (const c of this.partials.get(sha) ?? []) yield c;
  }
  async discardPartial(sha: string): Promise<void> {
    this.partials.delete(sha);
  }
  async commit(sha: string): Promise<void> {
    const chunks = this.partials.get(sha);
    if (!chunks) throw new Error(`sem parcial para ${sha}`);
    this.files.set(sha, concatBytes(...chunks));
    this.partials.delete(sha);
  }
  async has(sha: string): Promise<boolean> {
    return this.files.has(sha);
  }
  async list(): Promise<string[]> {
    return [...this.files.keys()];
  }
  async remove(sha: string): Promise<void> {
    this.files.delete(sha);
    this.partials.delete(sha);
  }
  async getUrl(sha: string): Promise<string> {
    if (!this.files.has(sha)) throw new Error(`mídia ausente: ${sha}`);
    return `memory://${sha}#${++this.urls}`;
  }
  releaseUrl(): void {
    /* nada */
  }
}

export class MemoryKeyStore implements KeyStore {
  private signer: Signer | null = null;
  async load(): Promise<Signer | null> {
    return this.signer;
  }
  async create(): Promise<Signer> {
    this.signer = new WebCryptoSigner(await generateEs256KeyPair(false));
    return this.signer;
  }
  async clear(): Promise<void> {
    this.signer = null;
  }
}
