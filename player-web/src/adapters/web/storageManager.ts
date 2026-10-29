/**
 * Persistência e quota do armazenamento do navegador (A4 Q22):
 *  - `navigator.storage.persist()` — sem isso todo armazenamento web é "best-effort" e pode ser despejado sob
 *    pressão de disco; se negado, o player exibe o alerta técnico "armazenamento não persistente".
 *  - `navigator.storage.estimate()` — consultado antes de planejar downloads.
 */

export interface PersistenceState {
  supported: boolean;
  persisted: boolean;
}

interface StorageLike {
  persist?: () => Promise<boolean>;
  persisted?: () => Promise<boolean>;
  estimate?: () => Promise<{ quota?: number; usage?: number }>;
}

function storage(): StorageLike | undefined {
  return (globalThis.navigator as unknown as { storage?: StorageLike } | undefined)?.storage;
}

export async function requestPersistence(): Promise<PersistenceState> {
  const s = storage();
  if (!s?.persist) return { supported: false, persisted: false };
  try {
    if (s.persisted && (await s.persisted())) return { supported: true, persisted: true };
    return { supported: true, persisted: await s.persist() };
  } catch {
    return { supported: true, persisted: false };
  }
}

export async function storageEstimate(): Promise<{ quota: number; usage: number } | null> {
  const s = storage();
  if (!s?.estimate) return null;
  try {
    const e = await s.estimate();
    if (typeof e.quota !== 'number' || typeof e.usage !== 'number') return null;
    return { quota: e.quota, usage: e.usage };
  } catch {
    return null;
  }
}
