import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryMediaStore, MemoryStateStore } from '../src/core/memoryStores';
import { STATE_KEYS } from '../src/core/storage';
import type { StoredManifest, SyncEvent } from '../src/core/sync';
import { manifestWithFiles, serverSide, syncWorld } from './helpers/world';

const types = (evs: SyncEvent[]) => evs.map((e) => e.type).filter((t) => t !== 'state' && t !== 'progress');
const states = (evs: SyncEvent[]) => evs.filter((e) => e.type === 'state').map((e) => (e as { state: string }).state);

afterEach(() => vi.useRealTimers());

/** Avança o tempo falso em passos, cedendo ao laço real (WebCrypto/streams do Node concluem entre os passos). */
async function advance(totalMs: number, stepMs = 1_000): Promise<void> {
  for (let t = 0; t < totalMs; t += stepMs) await vi.advanceTimersByTimeAsync(stepMs);
}

describe('SyncEngine — atualização atômica', () => {
  it('primeira versão: RECEIVED→VERIFY_SIG→PLAN→DOWNLOADING→VERIFY_SHA→STAGED→COMMIT→ACTIVE', async () => {
    const srv = await serverSide();
    const { manifest, files } = manifestWithFiles();
    const w = syncWorld({ keys: [srv.key], files });
    await w.sync.load();
    expect(await w.sync.receive(await srv.sign(manifest))).toBe('ACTIVATED');
    expect(states(w.events)).toEqual(['IDLE', 'RECEIVED', 'VERIFY_SIG', 'PLAN', 'DOWNLOADING', 'VERIFY_SHA', 'STAGED', 'COMMIT', 'ACTIVE']);
    expect(types(w.events)).toEqual(['staged', 'manifest_applied']);
    expect(w.sync.activeManifest()!.version).toBe(42);
    expect(w.sync.mediaReadyKeys(manifest).sort()).toEqual(Object.keys(manifest.media).sort());
    // persistido
    expect((await w.store.get<StoredManifest>(STATE_KEYS.manifestActive))!.version).toBe(42);
  });

  it('assinatura inválida (manifesto adulterado) ⇒ REJECTED, mantém a ativa, nada é baixado', async () => {
    const srv = await serverSide();
    const { manifest, files } = manifestWithFiles();
    const w = syncWorld({ keys: [srv.key], files });
    await w.sync.load();
    const signed = await srv.sign(manifest);
    signed.manifest = { ...signed.manifest, settings: { ...signed.manifest.settings, fade_ms: 0 } };
    expect(await w.sync.receive(signed)).toBe('REJECTED');
    expect(w.events.find((e) => e.type === 'manifest_failed')).toMatchObject({ reason: 'BAD_SIGNATURE', version: 42 });
    expect(w.sync.activeManifest()).toBeNull();
    expect(w.server.requests).toHaveLength(0);
  });

  it('chave de outro servidor / key_id desconhecido ⇒ REJECTED', async () => {
    const srv = await serverSide();
    const intruder = await serverSide('srv-1');
    const { manifest, files } = manifestWithFiles();
    const w = syncWorld({ keys: [srv.key], files });
    await w.sync.load();
    expect(await w.sync.receive(await intruder.sign(manifest))).toBe('REJECTED');
    expect(await w.sync.receive(await srv.sign(manifest, 'srv-9'))).toBe('REJECTED');
    expect(w.events.filter((e) => e.type === 'manifest_failed').map((e) => (e as { reason: string }).reason)).toEqual(['BAD_SIGNATURE', 'UNKNOWN_KEY']);
  });

  it('manifesto de outra tela ou malformado ⇒ REJECTED', async () => {
    const srv = await serverSide();
    const { manifest, files } = manifestWithFiles();
    const w = syncWorld({ keys: [srv.key], files, screenId: 'outra' });
    await w.sync.load();
    expect(await w.sync.receive(await srv.sign(manifest))).toBe('REJECTED');
    expect(await w.sync.receive({ alg: 'ES256', key_id: 'srv-1', signature: 'x', manifest: { schema: 'rd.manifest/1' } })).toBe('REJECTED');
    expect(w.events.filter((e) => e.type === 'manifest_failed').map((e) => (e as { reason: string }).reason)).toEqual([
      'SCREEN_MISMATCH',
      'MANIFEST_INVALID',
    ]);
  });

  it('nova versão fica STAGED; a ativa só muda no commit (o runtime chama na fronteira de ciclo); rollback volta', async () => {
    const srv = await serverSide();
    const v42 = manifestWithFiles();
    const v43 = manifestWithFiles({ version: 43, seedBase: 50 });
    const files = new Map([...v42.files, ...v43.files]);
    const w = syncWorld({ keys: [srv.key], files });
    await w.sync.load();
    await w.sync.receive(await srv.sign(v42.manifest));
    expect(await w.sync.receive(await srv.sign(v43.manifest))).toBe('STAGED');
    expect(w.sync.activeManifest()!.version).toBe(42);
    expect(w.sync.stagedManifest()!.version).toBe(43);
    expect(w.sync.pendingVersion).toBe(43);
    const committed = await w.sync.commit();
    expect(committed!.version).toBe(43);
    expect(w.sync.previousManifest()!.version).toBe(42);
    expect(w.events.filter((e) => e.type === 'manifest_applied').map((e) => (e as { version: number }).version)).toEqual([42, 43]);
    expect(await w.sync.rollback('teste')).toBe(true);
    expect(w.sync.activeManifest()!.version).toBe(42);
  });

  it('versão igual ou menor que a ativa/staged ⇒ NOOP (sem regressão por replay)', async () => {
    const srv = await serverSide();
    const v42 = manifestWithFiles();
    const w = syncWorld({ keys: [srv.key], files: v42.files });
    await w.sync.load();
    await w.sync.receive(await srv.sign(v42.manifest));
    expect(await w.sync.receive(await srv.sign(v42.manifest))).toBe('NOOP');
    const v41 = manifestWithFiles({ version: 41 });
    expect(await w.sync.receive(await srv.sign(v41.manifest))).toBe('NOOP');
    expect(w.sync.activeManifest()!.version).toBe(42);
  });

  it('commit falho (transação) não muda nada; a ativa continua', async () => {
    const srv = await serverSide();
    const v42 = manifestWithFiles();
    const v43 = manifestWithFiles({ version: 43 });
    const w = syncWorld({ keys: [srv.key], files: v42.files });
    await w.sync.load();
    await w.sync.receive(await srv.sign(v42.manifest));
    await w.sync.receive(await srv.sign(v43.manifest));
    w.store.failNextTransact = new Error('IDB abortado');
    expect(await w.sync.commit()).toBeNull();
    expect(w.sync.activeManifest()!.version).toBe(42);
    expect(w.sync.stagedManifest()!.version).toBe(43);
    expect(w.events.at(-1)).toMatchObject({ type: 'manifest_failed' });
  });

  it('sha256 divergente: arquivo NÃO é aceito nem ativado enquanto não confere; transitório ⇒ ativa só com o arquivo correto', async () => {
    vi.useFakeTimers();
    const srv = await serverSide();
    const v = manifestWithFiles();
    const target = v.manifest.media.m1!.media_id;
    const good = v.files.get(target)!;
    const files = new Map(v.files);
    files.set(target, new Uint8Array(good.length).fill(7)); // servidor entrega bytes errados
    const w = syncWorld({ keys: [srv.key], files });
    await w.sync.load();
    const p = w.sync.receive(await srv.sign(v.manifest));
    const hits = () => w.server.requests.filter((r) => r.url.endsWith(target)).length;
    for (let i = 0; i < 100 && hits() < 2; i++) await vi.advanceTimersByTimeAsync(1_000);
    expect(hits()).toBe(2);
    expect(w.sync.state).toBe('DOWNLOADING');
    expect(w.sync.activeManifest()).toBeNull(); // nada ativado com mídia não verificada
    expect(await w.media.has(v.manifest.media.m1!.sha256)).toBe(false);
    files.set(target, good); // servidor corrigido
    await advance(10 * 60_000);
    expect(await p).toBe('ACTIVATED');
    expect(w.sync.activeManifest()!.failed_media).toEqual([]);
    expect(w.media.files.get(v.manifest.media.m1!.sha256)).toEqual(good);
  });

  it('falha permanente (5 tentativas ≤ 30 min) ⇒ ativa SEM a mídia (vira fallback) e emite media_error', async () => {
    vi.useFakeTimers();
    const srv = await serverSide();
    const v = manifestWithFiles();
    const target = v.manifest.media.m2!.media_id;
    const files = new Map(v.files);
    files.set(target, new Uint8Array(v.files.get(target)!.length).fill(1)); // sempre divergente
    const w = syncWorld({ keys: [srv.key], files });
    await w.sync.load();
    const p = w.sync.receive(await srv.sign(v.manifest));
    await advance(30 * 60_000);
    expect(await p).toBe('ACTIVATED');
    const active = w.sync.activeManifest()!;
    expect(active.failed_media).toEqual(['m2']);
    expect(w.sync.mediaReadyKeys(v.manifest)).not.toContain('m2');
    const err = w.events.find((e) => e.type === 'media_error') as { media_id: string; attempts: number };
    expect(err).toMatchObject({ media_id: target, attempts: 5 });
    expect(w.server.requests.filter((r) => r.url.endsWith(target))).toHaveLength(5);
  });

  it('versão mais nova durante o download cancela a anterior (SUPERSEDED) e prossegue', async () => {
    const srv = await serverSide();
    const v42 = manifestWithFiles();
    const v43 = manifestWithFiles({ version: 43 });
    let open!: () => void;
    const gate = new Promise<void>((r) => (open = r));
    const w = syncWorld({ keys: [srv.key], files: v42.files, serverOpts: { gate } });
    await w.sync.load();
    const p42 = w.sync.receive(await srv.sign(v42.manifest));
    while (w.server.requests.length === 0) await new Promise((r) => setTimeout(r, 2));
    const p43 = w.sync.receive(await srv.sign(v43.manifest));
    const v43Planning = () => w.events.some((e) => e.type === 'state' && e.state === 'PLAN' && e.version === 43);
    while (!v43Planning()) await new Promise((r) => setTimeout(r, 2));
    open();
    expect(await p42).toBe('SUPERSEDED');
    expect(await p43).toBe('ACTIVATED');
    expect(w.sync.activeManifest()!.version).toBe(43);
  });

  it('PLAN consulta a quota: sem espaço ⇒ DISK_FULL, mantém a ativa', async () => {
    const srv = await serverSide();
    const v = manifestWithFiles();
    const w = syncWorld({ keys: [srv.key], files: v.files, estimate: async () => ({ quota: 1_000_000, usage: 999_000 }) });
    await w.sync.load();
    expect(await w.sync.receive(await srv.sign(v.manifest))).toBe('FAILED');
    expect(types(w.events)).toEqual(['disk_full', 'manifest_failed']);
    expect(w.server.requests).toHaveLength(0);
  });

  it('"reboot": nova instância restaura ativa/staged do armazenamento sem rede', async () => {
    const srv = await serverSide();
    const v42 = manifestWithFiles();
    const v43 = manifestWithFiles({ version: 43, seedBase: 70 });
    const store = new MemoryStateStore();
    const media = new MemoryMediaStore();
    const w = syncWorld({ keys: [srv.key], files: new Map([...v42.files, ...v43.files]), store, media });
    await w.sync.load();
    await w.sync.receive(await srv.sign(v42.manifest));
    await w.sync.receive(await srv.sign(v43.manifest));
    const w2 = syncWorld({ keys: [srv.key], files: new Map(), store, media });
    await w2.sync.load();
    expect(w2.sync.activeManifest()!.version).toBe(42);
    expect(w2.sync.stagedManifest()!.version).toBe(43);
    // staged com mídia sumida é descartado (será baixado de novo)
    await media.remove(v43.manifest.media.m1!.sha256);
    const w3 = syncWorld({ keys: [srv.key], files: new Map(), store, media });
    await w3.sync.load();
    expect(w3.sync.stagedManifest()).toBeNull();
  });

  it('GC remove só mídias fora de {ativa, staged, anterior} ou vencidas', async () => {
    const srv = await serverSide();
    const v42 = manifestWithFiles({ seedBase: 11 });
    const v43 = manifestWithFiles({ version: 43, seedBase: 31 });
    const v44 = manifestWithFiles({ version: 44, seedBase: 51 });
    const w = syncWorld({ keys: [srv.key], files: new Map([...v42.files, ...v43.files, ...v44.files]) });
    await w.sync.load();
    await w.sync.receive(await srv.sign(v42.manifest));
    await w.sync.receive(await srv.sign(v43.manifest));
    await w.sync.commit();
    await w.sync.gc();
    // anterior (42) ainda guardada para rollback
    expect(await w.media.has(v42.manifest.media.m1!.sha256)).toBe(true);
    await w.sync.receive(await srv.sign(v44.manifest));
    await w.sync.commit();
    await w.sync.gc();
    expect(await w.media.has(v42.manifest.media.m1!.sha256)).toBe(false);
    expect(await w.media.has(v43.manifest.media.m1!.sha256)).toBe(true);
    expect(await w.media.has(v44.manifest.media.m1!.sha256)).toBe(true);
    // vencida: criativo único da mídia expirou ⇒ removida mesmo referenciada
    w.sync.activeManifest()!.signed.manifest.creatives.c1!.valid_until_ms = 1;
    w.sync.previousManifest()!.signed.manifest.creatives.c1!.valid_until_ms = 1;
    const removed = await w.sync.gc();
    expect(removed).toContain(v44.manifest.media.m1!.sha256);
    expect(removed).toContain(v43.manifest.media.m1!.sha256);
  });

  it('repair: baixa depois a mídia que falhou e avisa media_ready', async () => {
    vi.useFakeTimers();
    const srv = await serverSide();
    const v = manifestWithFiles();
    const target = v.manifest.media.m2!.media_id;
    const files = new Map(v.files);
    const good = files.get(target)!;
    files.delete(target); // 404
    const w = syncWorld({ keys: [srv.key], files });
    await w.sync.load();
    const p = w.sync.receive(await srv.sign(v.manifest));
    await advance(30 * 60_000);
    await p;
    expect(w.sync.activeManifest()!.failed_media).toEqual(['m2']);
    files.set(target, good);
    const r = w.sync.repair();
    await advance(5_000);
    expect(await r).toEqual(['m2']);
    expect(w.sync.activeManifest()!.failed_media).toEqual([]);
    expect(w.events.at(-1)).toEqual({ type: 'media_ready', keys: ['m2'] });
  });
});
