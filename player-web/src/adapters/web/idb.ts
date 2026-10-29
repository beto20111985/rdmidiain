/**
 * IndexedDB mínimo (sem dependências) — banco "rd-player".
 *  kv          : estado (manifesto ativo/staged/anterior, âncora, cadeia do PoP, kills, comandos, relógio…)
 *  outbox      : eventos PoP assinados pendentes (keyPath "oid")
 *  keys        : par ES256 do dispositivo (CryptoKey não exportável — clonável estruturalmente)
 *  media_meta  : metadados da mídia no fallback sem OPFS
 *  media_chunks: blocos Blob da mídia no fallback sem OPFS (chave [sha, idx])
 */

export const DB_NAME = 'rd-player';
export const DB_VERSION = 1;

export function reqP<T>(r: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error ?? new Error('IDBRequest falhou'));
  });
}

export function txDone(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error ?? new Error('IDBTransaction falhou'));
    tx.onabort = () => reject(tx.error ?? new Error('IDBTransaction abortada'));
  });
}

export function openPlayerDb(factory: IDBFactory = globalThis.indexedDB, name = DB_NAME): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    if (!factory) {
      reject(new Error('IndexedDB indisponível'));
      return;
    }
    const r = factory.open(name, DB_VERSION);
    r.onupgradeneeded = () => {
      const db = r.result;
      if (!db.objectStoreNames.contains('kv')) db.createObjectStore('kv');
      if (!db.objectStoreNames.contains('outbox')) db.createObjectStore('outbox', { keyPath: 'oid' });
      if (!db.objectStoreNames.contains('keys')) db.createObjectStore('keys');
      if (!db.objectStoreNames.contains('media_meta')) db.createObjectStore('media_meta', { keyPath: 'sha' });
      if (!db.objectStoreNames.contains('media_chunks')) db.createObjectStore('media_chunks', { keyPath: ['sha', 'idx'] });
    };
    r.onsuccess = () => {
      const db = r.result;
      // Outra aba/versão pedindo upgrade: fecha para não travar.
      db.onversionchange = () => db.close();
      resolve(db);
    };
    r.onerror = () => reject(r.error ?? new Error('falha ao abrir IndexedDB'));
    r.onblocked = () => reject(new Error('IndexedDB bloqueado por outra instância'));
  });
}
