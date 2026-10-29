import { generateEs256KeyPair, signCanonical, WebCryptoSigner, type Signer } from '../../src/core/crypto';
import { DownloadQueue, downloadFile, type DownloadQueueOptions } from '../../src/core/downloader';
import { MemoryMediaStore, MemoryStateStore } from '../../src/core/memoryStores';
import type { Manifest, ServerKey, SignedManifest } from '../../src/core/protocol';
import { SyncEngine, type SyncEvent } from '../../src/core/sync';
import { b64uEncode } from '../../src/core/util';
import { bytesOf, fakeMediaServer, shaHex, type FakeMediaServerOptions } from './fakeServer';
import { mk, type MkOptions } from './manifest';

export interface ServerSide {
  signer: Signer;
  key: ServerKey;
  sign(m: Manifest, keyId?: string): Promise<SignedManifest>;
}

export async function serverSide(keyId = 'srv-1'): Promise<ServerSide> {
  const signer = new WebCryptoSigner(await generateEs256KeyPair(false));
  const key: ServerKey = { key_id: keyId, public_key: b64uEncode(await signer.publicKeySpki()), alg: 'ES256' };
  return {
    signer,
    key,
    async sign(m, kid = keyId) {
      return { alg: 'ES256', key_id: kid, signature: await signCanonical(signer, m), manifest: m };
    },
  };
}

/** Manifesto do helper com mídias REAIS (bytes servidos pelo servidor falso e sha256 corretos). */
export function manifestWithFiles(o: MkOptions & { seedBase?: number; sizes?: Record<string, number> } = {}) {
  const m = mk(o);
  const files = new Map<string, Uint8Array>();
  let seed = o.seedBase ?? 11;
  for (const [key, e] of Object.entries(m.media)) {
    const data = bytesOf(o.sizes?.[key] ?? 4_000 + seed * 10, seed++);
    e.sha256 = shaHex(data);
    e.size = data.length;
    e.media_id = `00000000-0000-4000-8000-${String(seed - 1).padStart(12, '0')}`;
    files.set(e.media_id, data);
  }
  return { manifest: m, files };
}

export interface SyncWorld {
  store: MemoryStateStore;
  media: MemoryMediaStore;
  events: SyncEvent[];
  sync: SyncEngine;
  server: ReturnType<typeof fakeMediaServer>;
  files: Map<string, Uint8Array>;
}

export function syncWorld(opts: {
  keys: ServerKey[];
  files: Map<string, Uint8Array>;
  serverOpts?: FakeMediaServerOptions;
  store?: MemoryStateStore;
  media?: MemoryMediaStore;
  queue?: Partial<DownloadQueueOptions>;
  estimate?: () => Promise<{ quota: number; usage: number } | null>;
  screenId?: string;
}): SyncWorld {
  const store = opts.store ?? new MemoryStateStore();
  const media = opts.media ?? new MemoryMediaStore();
  const events: SyncEvent[] = [];
  const server = fakeMediaServer(opts.files, opts.serverOpts);
  const queue = new DownloadQueue({
    executor: async (job, ctx) => {
      await downloadFile(job, { fetch: server.fetch, url: `/api/v1/player/media/${job.media_id}`, headers: {}, store: media, signal: ctx.signal, onProgress: ctx.onProgress });
    },
    now: () => Date.now(),
    ...opts.queue,
  });
  const sync = new SyncEngine({
    store,
    media,
    queue,
    serverKeys: () => opts.keys,
    screenId: () => opts.screenId ?? 'scr-1',
    estimate: opts.estimate,
    now: () => Date.now(),
    emit: (e) => events.push(e),
  });
  return { store, media, events, sync, server, files: opts.files };
}
