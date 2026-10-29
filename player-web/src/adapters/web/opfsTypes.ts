/**
 * Tipos mínimos do OPFS (File System Access / Origin Private File System), declarados localmente para não
 * depender da versão da lib DOM do TypeScript nem misturar as libs DOM e WebWorker.
 */

export interface OpfsWritable {
  write(data: BufferSource | Blob): Promise<void>;
  seek(position: number): Promise<void>;
  truncate(size: number): Promise<void>;
  close(): Promise<void>;
  abort?(): Promise<void>;
}

export interface OpfsSyncHandle {
  read(buffer: ArrayBufferView, opts?: { at?: number }): number;
  write(buffer: ArrayBufferView, opts?: { at?: number }): number;
  truncate(size: number): void;
  getSize(): number;
  flush(): void;
  close(): void;
}

export interface OpfsFileHandle {
  readonly kind: 'file';
  readonly name: string;
  getFile(): Promise<File>;
  createWritable?(opts?: { keepExistingData?: boolean }): Promise<OpfsWritable>;
  createSyncAccessHandle?(): Promise<OpfsSyncHandle>;
  move?(newName: string): Promise<void>;
}

export interface OpfsDirHandle {
  readonly kind: 'directory';
  getFileHandle(name: string, opts?: { create?: boolean }): Promise<OpfsFileHandle>;
  getDirectoryHandle(name: string, opts?: { create?: boolean }): Promise<OpfsDirHandle>;
  removeEntry(name: string, opts?: { recursive?: boolean }): Promise<void>;
  keys(): AsyncIterableIterator<string>;
}

export async function opfsRoot(): Promise<OpfsDirHandle | null> {
  const storage = (globalThis.navigator as unknown as { storage?: { getDirectory?: () => Promise<OpfsDirHandle> } } | undefined)?.storage;
  if (!storage?.getDirectory) return null;
  try {
    return await storage.getDirectory();
  } catch {
    return null;
  }
}

export async function mediaDir(): Promise<OpfsDirHandle | null> {
  const root = await opfsRoot();
  if (!root) return null;
  try {
    return await root.getDirectoryHandle('media', { create: true });
  } catch {
    return null;
  }
}

export function isNotFound(e: unknown): boolean {
  return e instanceof Error && (e.name === 'NotFoundError' || e.name === 'TypeMismatchError');
}

export const PART = '.part';
export const SHA_RE = /^[0-9a-f]{64}$/;
