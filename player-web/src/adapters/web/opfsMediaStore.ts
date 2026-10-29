/**
 * Mídia no OPFS, endereçada por sha256 (ADR_008; A4 Q8):
 *   media/<sha256>.part  — download em andamento (escrita por offset; retomável com Range)
 *   media/<sha256>       — arquivo definitivo (só após sha256 conferido)
 * Modo "sync" (Web Worker): FileSystemSyncAccessHandle grava no lugar, com flush periódico ⇒ progresso durável
 * mesmo se a página cair. Modo "writable" (thread principal): createWritable({keepExistingData}) — o progresso
 * só fica durável no close() (limitação documentada); é o fallback quando não há Worker/SyncAccessHandle.
 * Reprodução: getFile() devolve um File em disco ⇒ blob: URL sem carregar 200 MB na RAM.
 */

import type { MediaStore, MediaWriter } from '../../core/storage';
import { isNotFound, mediaDir, PART, SHA_RE, type OpfsDirHandle, type OpfsFileHandle } from './opfsTypes';

const READ_CHUNK = 1024 * 1024;
const FLUSH_EVERY = 4 * 1024 * 1024;

export type OpfsMode = 'sync' | 'writable';

export class OpfsMediaStore implements MediaStore {
  readonly kind: string;

  constructor(
    private readonly dir: OpfsDirHandle,
    private readonly mode: OpfsMode,
  ) {
    this.kind = `opfs-${mode}`;
  }

  static async open(mode: OpfsMode): Promise<OpfsMediaStore | null> {
    const dir = await mediaDir();
    return dir ? new OpfsMediaStore(dir, mode) : null;
  }

  private async handle(name: string, create = false): Promise<OpfsFileHandle | null> {
    try {
      return await this.dir.getFileHandle(name, { create });
    } catch (e) {
      if (isNotFound(e)) return null;
      throw e;
    }
  }

  async partialSize(sha: string): Promise<number> {
    const h = await this.handle(sha + PART);
    return h ? (await h.getFile()).size : 0;
  }

  async openWriter(sha: string, offset: number): Promise<MediaWriter> {
    const fh = (await this.handle(sha + PART, true))!;
    if (this.mode === 'sync' && fh.createSyncAccessHandle) {
      const h = await fh.createSyncAccessHandle();
      try {
        h.truncate(offset);
      } catch (e) {
        h.close();
        throw e;
      }
      let pos = offset;
      let sinceFlush = 0;
      let closed = false;
      return {
        write: async (chunk) => {
          let written = 0;
          while (written < chunk.length) {
            const n = h.write(chunk.subarray(written), { at: pos + written });
            if (n <= 0) throw new Error('OPFS: escrita sem progresso');
            written += n;
          }
          pos += chunk.length;
          sinceFlush += chunk.length;
          if (sinceFlush >= FLUSH_EVERY) {
            h.flush();
            sinceFlush = 0;
          }
        },
        close: async () => {
          if (closed) return;
          closed = true;
          try {
            h.flush();
          } finally {
            h.close();
          }
        },
      };
    }
    if (!fh.createWritable) throw new Error('OPFS sem API de escrita nesta thread');
    const w = await fh.createWritable({ keepExistingData: true });
    await w.truncate(offset);
    await w.seek(offset);
    let closed = false;
    return {
      write: async (chunk) => {
        await w.write(chunk);
      },
      close: async () => {
        if (closed) return;
        closed = true;
        await w.close();
      },
    };
  }

  async *readPartial(sha: string): AsyncIterable<Uint8Array> {
    const h = await this.handle(sha + PART);
    if (!h) return;
    const file = await h.getFile();
    for (let o = 0; o < file.size; o += READ_CHUNK) {
      yield new Uint8Array(await file.slice(o, Math.min(file.size, o + READ_CHUNK)).arrayBuffer());
    }
  }

  async discardPartial(sha: string): Promise<void> {
    await this.removeName(sha + PART);
  }

  async commit(sha: string): Promise<void> {
    const part = await this.handle(sha + PART);
    if (!part) throw new Error(`OPFS: parcial ausente para ${sha}`);
    await this.removeName(sha);
    if (part.move) {
      try {
        await part.move(sha);
        return;
      } catch {
        /* cai na cópia */
      }
    }
    // Sem move(): copia em blocos e remove o parcial.
    const src = await part.getFile();
    const dst = (await this.handle(sha, true))!;
    if (this.mode === 'sync' && dst.createSyncAccessHandle) {
      const h = await dst.createSyncAccessHandle();
      try {
        h.truncate(0);
        for (let o = 0; o < src.size; o += READ_CHUNK) {
          const buf = new Uint8Array(await src.slice(o, Math.min(src.size, o + READ_CHUNK)).arrayBuffer());
          h.write(buf, { at: o });
        }
        h.flush();
      } finally {
        h.close();
      }
    } else if (dst.createWritable) {
      const w = await dst.createWritable({ keepExistingData: false });
      for (let o = 0; o < src.size; o += READ_CHUNK) await w.write(src.slice(o, Math.min(src.size, o + READ_CHUNK)));
      await w.close();
    } else {
      throw new Error('OPFS sem API de escrita para o commit');
    }
    await this.removeName(sha + PART);
  }

  async has(sha: string): Promise<boolean> {
    return (await this.handle(sha)) !== null;
  }

  async list(): Promise<string[]> {
    const out: string[] = [];
    for await (const name of this.dir.keys()) if (SHA_RE.test(name)) out.push(name);
    return out;
  }

  async remove(sha: string): Promise<void> {
    await this.removeName(sha);
    await this.removeName(sha + PART);
  }

  async getUrl(sha: string): Promise<string> {
    const h = await this.handle(sha);
    if (!h) throw new Error(`mídia ausente: ${sha}`);
    return URL.createObjectURL(await h.getFile());
  }

  releaseUrl(url: string): void {
    URL.revokeObjectURL(url);
  }

  private async removeName(name: string): Promise<void> {
    try {
      await this.dir.removeEntry(name);
    } catch (e) {
      if (!isNotFound(e)) throw e;
    }
  }
}
