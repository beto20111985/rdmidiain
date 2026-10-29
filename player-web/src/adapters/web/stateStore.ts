/** StateStore sobre IndexedDB: transações atômicas para commit de manifesto e cadeia do PoP. */

import type { OutboxRecord, StateStore, StateTransaction } from '../../core/storage';
import { reqP, txDone } from './idb';

export class IdbStateStore implements StateStore {
  constructor(private readonly db: IDBDatabase) {}

  async get<T>(key: string): Promise<T | undefined> {
    const tx = this.db.transaction('kv', 'readonly');
    return (await reqP(tx.objectStore('kv').get(key))) as T | undefined;
  }

  async set(key: string, value: unknown): Promise<void> {
    const tx = this.db.transaction('kv', 'readwrite');
    tx.objectStore('kv').put(value, key);
    await txDone(tx);
  }

  async delete(key: string): Promise<void> {
    const tx = this.db.transaction('kv', 'readwrite');
    tx.objectStore('kv').delete(key);
    await txDone(tx);
  }

  /** Tudo ou nada: uma única transação readwrite sobre kv + outbox. */
  async transact(t: StateTransaction): Promise<void> {
    const tx = this.db.transaction(['kv', 'outbox'], 'readwrite');
    const done = txDone(tx);
    try {
      const kv = tx.objectStore('kv');
      const ob = tx.objectStore('outbox');
      for (const [k, v] of Object.entries(t.set ?? {})) kv.put(v, k);
      for (const k of t.del ?? []) kv.delete(k);
      for (const r of t.outboxPut ?? []) ob.put(r);
      for (const id of t.outboxDelete ?? []) ob.delete(id);
    } catch (e) {
      // Exceção SÍNCRONA ao criar um pedido não aborta a transação sozinha: aborta explicitamente (tudo ou nada).
      try {
        tx.abort();
      } catch {
        /* já finalizada */
      }
      done.catch(() => undefined);
      throw e;
    }
    await done;
  }

  async outboxList(limit: number): Promise<OutboxRecord[]> {
    const tx = this.db.transaction('outbox', 'readonly');
    const store = tx.objectStore('outbox');
    if (typeof store.getAll === 'function') return (await reqP(store.getAll(undefined, limit))) as OutboxRecord[];
    const out: OutboxRecord[] = [];
    await new Promise<void>((resolve, reject) => {
      const cur = store.openCursor();
      cur.onsuccess = () => {
        const c = cur.result;
        if (!c || out.length >= limit) return resolve();
        out.push(c.value as OutboxRecord);
        c.continue();
      };
      cur.onerror = () => reject(cur.error);
    });
    return out;
  }

  async outboxCount(): Promise<number> {
    const tx = this.db.transaction('outbox', 'readonly');
    return reqP(tx.objectStore('outbox').count());
  }

  async outboxClear(): Promise<void> {
    const tx = this.db.transaction('outbox', 'readwrite');
    tx.objectStore('outbox').clear();
    await txDone(tx);
  }
}
