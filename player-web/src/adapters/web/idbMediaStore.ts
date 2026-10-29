/**
 * Fallback de mídia sem OPFS (Smart TVs / navegadores antigos): blocos Blob em IndexedDB.
 * Blobs guardados no IndexedDB ficam em disco; a URL de reprodução é um Blob composto pelos blocos
 * (o navegador referencia os blocos em disco, sem concatenar tudo na RAM).
 */

import type { MediaStore, MediaWriter } from '../../core/storage';
import { reqP, txDone } from './idb';

const BLOCK = 1024 * 1024;

interface Meta {
  sha: string;
  /** bytes gravados */
  size: number;
  chunks: number;
  committed: boolean;
}

interface Chunk {
  sha: string;
  idx: number;
  blob: Blob;
  size: number;
}

export class IdbMediaStore implements MediaStore {
  readonly kind = 'idb-blob';

  constructor(private readonly db: IDBDatabase) {}

  private async meta(sha: string): Promise<Meta | undefined> {
    const tx = this.db.transaction('media_meta', 'readonly');
    return (await reqP(tx.objectStore('media_meta').get(sha))) as Meta | undefined;
  }

  private async chunks(sha: string): Promise<Chunk[]> {
    const tx = this.db.transaction('media_chunks', 'readonly');
    const range = IDBKeyRange.bound([sha, 0], [sha, Number.MAX_SAFE_INTEGER]);
    return (await reqP(tx.objectStore('media_chunks').getAll(range))) as Chunk[];
  }

  async partialSize(sha: string): Promise<number> {
    const m = await this.meta(sha);
    return m && !m.committed ? m.size : 0;
  }

  async openWriter(sha: string, offset: number): Promise<MediaWriter> {
    let m = await this.meta(sha);
    if (offset === 0) {
      await this.deleteAll(sha);
      m = { sha, size: 0, chunks: 0, committed: false };
      await this.putMeta(m);
    } else if (!m || m.committed || m.size !== offset) {
      // O downloader só retoma a partir de partialSize() (fim do parcial) ou recomeça do zero.
      throw new Error('IDB: retomada só a partir do fim do parcial');
    }
    const meta = m;
    let pending: Uint8Array[] = [];
    let pendingBytes = 0;
    const flush = async (): Promise<void> => {
      if (pendingBytes === 0) return;
      const blob = new Blob(pending as BlobPart[]);
      const tx = this.db.transaction(['media_chunks', 'media_meta'], 'readwrite');
      tx.objectStore('media_chunks').put({ sha, idx: meta.chunks, blob, size: pendingBytes } satisfies Chunk);
      meta.chunks += 1;
      meta.size += pendingBytes;
      tx.objectStore('media_meta').put(meta);
      pending = [];
      pendingBytes = 0;
      await txDone(tx);
    };
    return {
      write: async (chunk) => {
        pending.push(chunk.slice());
        pendingBytes += chunk.length;
        if (pendingBytes >= BLOCK) await flush();
      },
      close: async () => {
        await flush();
      },
    };
  }

  async *readPartial(sha: string): AsyncIterable<Uint8Array> {
    const list = (await this.chunks(sha)).sort((a, b) => a.idx - b.idx);
    for (const c of list) yield new Uint8Array(await c.blob.arrayBuffer());
  }

  async discardPartial(sha: string): Promise<void> {
    const m = await this.meta(sha);
    if (m && !m.committed) await this.deleteAll(sha);
  }

  async commit(sha: string): Promise<void> {
    const m = await this.meta(sha);
    if (!m) throw new Error(`IDB: parcial ausente para ${sha}`);
    m.committed = true;
    await this.putMeta(m);
  }

  async has(sha: string): Promise<boolean> {
    return (await this.meta(sha))?.committed === true;
  }

  async list(): Promise<string[]> {
    const tx = this.db.transaction('media_meta', 'readonly');
    const all = (await reqP(tx.objectStore('media_meta').getAll())) as Meta[];
    return all.filter((m) => m.committed).map((m) => m.sha);
  }

  async remove(sha: string): Promise<void> {
    await this.deleteAll(sha);
  }

  async getUrl(sha: string): Promise<string> {
    if (!(await this.has(sha))) throw new Error(`mídia ausente: ${sha}`);
    const list = (await this.chunks(sha)).sort((a, b) => a.idx - b.idx);
    return URL.createObjectURL(new Blob(list.map((c) => c.blob)));
  }

  releaseUrl(url: string): void {
    URL.revokeObjectURL(url);
  }

  private async putMeta(m: Meta): Promise<void> {
    const tx = this.db.transaction('media_meta', 'readwrite');
    tx.objectStore('media_meta').put(m);
    await txDone(tx);
  }

  private async deleteAll(sha: string): Promise<void> {
    const tx = this.db.transaction(['media_chunks', 'media_meta'], 'readwrite');
    tx.objectStore('media_chunks').delete(IDBKeyRange.bound([sha, 0], [sha, Number.MAX_SAFE_INTEGER]));
    tx.objectStore('media_meta').delete(sha);
    await txDone(tx);
  }
}
