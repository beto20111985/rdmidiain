import 'fake-indexeddb/auto';
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { openPlayerDb } from '../src/adapters/web/idb';
import { IdbMediaStore } from '../src/adapters/web/idbMediaStore';
import { IdbKeyStore } from '../src/adapters/web/keyStore';
import { createBridge, NativeBridge, WebBridge, type NativeTransport } from '../src/adapters/web/platform';
import { IdbStateStore } from '../src/adapters/web/stateStore';
import { deriveWsBase, loadConfig } from '../src/config';
import { importSpkiVerifier, verifyCanonical, signCanonical } from '../src/core/crypto';
import { downloadFile } from '../src/core/downloader';
import { PopRecorder } from '../src/core/pop';
import { STATE_KEYS } from '../src/core/storage';
import { bytesOf, fakeMediaServer, shaHex } from './helpers/fakeServer';

let dbn = 0;
const freshDb = () => openPlayerDb(indexedDB, `rd-player-test-${++dbn}`);

describe('IndexedDB — estado, outbox, chave', () => {
  it('transação atômica: falha no meio não aplica nada', async () => {
    const st = new IdbStateStore(await freshDb());
    await st.set('a', 1);
    // outboxPut sem a chave "oid" (keyPath) faz a transação abortar inteira
    await expect(st.transact({ set: { a: 2, b: 3 }, outboxPut: [{} as never] })).rejects.toBeTruthy();
    expect(await st.get('a')).toBe(1);
    expect(await st.get('b')).toBeUndefined();
  });

  it('PoP sobre IndexedDB: seq/prev_hash persistem e o outbox sai em ordem', async () => {
    const db = await freshDb();
    const st = new IdbStateStore(db);
    const keys = new IdbKeyStore(db);
    const signer = await keys.create();
    const mk = () => new PopRecorder({ store: new IdbStateStore(db), signer: () => signer, identity: () => ({ device_id: 'd', screen_id: 's' }) });
    const base = { creative: { creative_id: 'c', code: 'RD-A7M2P9', campaign_id: null, kind: 'INTERNAL' as const }, insertion_id: null, manifest_version: 1, expected_ms: 30_000, actual_ms: 30_000, result: 'COMPLETED' as const, clock_offset_ms: 0, offline: true };
    await mk().record({ ...base, started_at_ms: 1_000_000 });
    await mk().record({ ...base, started_at_ms: 1_030_000 }); // "reboot"
    const list = await st.outboxList(10);
    expect(list.map((r) => r.event.seq)).toEqual([1, 2]);
    expect((await st.get<{ seq: number }>(STATE_KEYS.popChain))!.seq).toBe(2);
    const verifier = await importSpkiVerifier(await signer.publicKeySpki());
    expect(await verifyCanonical(verifier, list[1]!.event, list[1]!.signature)).toBe(true);
  });

  it('chave ES256 guardada como CryptoKey NÃO exportável e recarregada após "reboot"', async () => {
    const db = await freshDb();
    const k1 = new IdbKeyStore(db);
    const s1 = await k1.create();
    const k2 = new IdbKeyStore(db);
    const s2 = (await k2.load())!;
    expect(s2).not.toBeNull();
    expect(Buffer.from(await s2.publicKeySpki())).toEqual(Buffer.from(await s1.publicKeySpki()));
    const sig = await signCanonical(s2, { x: 1 });
    expect(await verifyCanonical(await importSpkiVerifier(await s1.publicKeySpki()), { x: 1 }, sig)).toBe(true);
    await k2.clear();
    expect(await new IdbKeyStore(db).load()).toBeNull();
  });
});

describe('mídia em IndexedDB (fallback sem OPFS)', () => {
  it('download com retomada grava blocos Blob, confere sha e serve URL', async () => {
    const store = new IdbMediaStore(await freshDb());
    const data = bytesOf(2_600_000, 5);
    const sha = shaHex(data);
    const srv = fakeMediaServer(new Map([['m', data]]), { cutAfter: (i) => (i === 0 ? 1_500_000 : null), chunk: 100_000 });
    const job = { key: 'm', media_id: 'm', sha256: sha, size: data.length, mime: 'video/mp4' };
    await expect(downloadFile(job, { fetch: srv.fetch, url: '/m', headers: {}, store })).rejects.toThrow(/NETWORK/);
    const partial = await store.partialSize(sha);
    expect(partial).toBeGreaterThan(0);
    expect(partial).toBeLessThanOrEqual(1_500_000);
    await downloadFile(job, { fetch: srv.fetch, url: '/m', headers: {}, store });
    expect(srv.requests[1]!.headers.range).toBe(`bytes=${partial}-`);
    expect(await store.has(sha)).toBe(true);
    expect(await store.list()).toEqual([sha]);
    const url = await store.getUrl(sha);
    expect(url).toMatch(/^blob:/);
    store.releaseUrl(url);
    await store.remove(sha);
    expect(await store.has(sha)).toBe(false);
  });
});

describe('ponte rd-native/1', () => {
  class FakeWebview implements NativeTransport {
    sent: Record<string, unknown>[] = [];
    private fn: ((ev: { data: unknown }) => void) | null = null;
    auto: ((m: Record<string, unknown>) => Record<string, unknown> | null) | null = null;
    postMessage(msg: unknown) {
      const m = msg as Record<string, unknown>;
      this.sent.push(m);
      const reply = this.auto?.(m);
      if (reply) queueMicrotask(() => this.fn?.({ data: reply }));
    }
    addEventListener(_t: 'message', fn: (ev: { data: unknown }) => void) {
      this.fn = fn;
    }
    emit(data: unknown) {
      this.fn?.({ data });
    }
  }
  const native = { bridge: 'rd-native/1', platform: 'windows', shell_version: '1.2.3', capabilities: ['telemetry', 'reboot'], config: { api_base_url: 'https://api.x', config_ok: true } };

  it('request/response com envelope {rd:1}, telemetria e comando', async () => {
    const wv = new FakeWebview();
    wv.auto = (m) =>
      m.kind === 'request'
        ? {
            rd: 1,
            kind: 'response',
            id: m.id,
            ok: true,
            result: m.method === 'get_telemetry' ? { cpu_pct: 12, ram_used_mb: 900, wifi: { ssid: 'loja', rssi_dbm: -55 }, platform: 'windows' } : { status: 'ACCEPTED', delay_ms: 2000 },
          }
        : null;
    const b = new NativeBridge(native, wv);
    const t = await b.getTelemetry();
    expect(t).toMatchObject({ cpu_pct: 12, ram_used_mb: 900, wifi: { ssid: 'loja', rssi_dbm: -55 } });
    expect(wv.sent[0]).toMatchObject({ rd: 1, kind: 'request', method: 'get_telemetry', params: {} });
    const r = await b.command({ command_id: 'c1', command: 'reboot', args: null });
    expect(r).toEqual({ status: 'ACCEPTED', delay_ms: 2000, detail: null });
    expect(b.platform).toBe('windows');
    expect(b.shellVersion).toBe('1.2.3');
  });

  it('erro da casca vira UNSUPPORTED/FAILED; sem resposta ⇒ TIMEOUT', async () => {
    const wv = new FakeWebview();
    wv.auto = (m) => ({ rd: 1, kind: 'response', id: m.id, ok: false, error: { code: 'UNSUPPORTED', message: 'sem reboot' } });
    const b = new NativeBridge(native, wv, 50);
    expect((await b.command({ command_id: 'x', command: 'reboot', args: null })).status).toBe('UNSUPPORTED');
    const silent = new NativeBridge(native, new FakeWebview(), 30);
    await expect(silent.request('get_info')).rejects.toThrow(/TIMEOUT/);
  });

  it('notify ready/progress/log e eventos (inclui string JSON)', async () => {
    const wv = new FakeWebview();
    const b = new NativeBridge(native, wv);
    b.notifyReady('1.0.0');
    b.notifyProgress({ state: 'PLAYING', creative_code: 'RD-A7M2P9', item_started_at_ms: 5 });
    expect(wv.sent).toEqual([
      { rd: 1, kind: 'notify', method: 'ready', params: { bundle_version: '1.0.0' } },
      { rd: 1, kind: 'notify', method: 'progress', params: { state: 'PLAYING', creative_code: 'RD-A7M2P9', item_started_at_ms: 5 } },
    ]);
    wv.sent.length = 0;
    // tech_pin: só os campos do registro PBKDF2 saem pela ponte (um `pin` extra nunca é repassado).
    const rec = { algo: 'pbkdf2-sha256', iterations: 210_000, salt_b64u: 'c2FsdA', hash_b64u: 'aGFzaA', set_at_ms: 7, pin: '1234' };
    b.notifyTechPin(rec as unknown as Parameters<typeof b.notifyTechPin>[0]);
    expect(wv.sent).toEqual([
      {
        rd: 1,
        kind: 'notify',
        method: 'tech_pin',
        params: { algo: 'pbkdf2-sha256', iterations: 210_000, salt_b64u: 'c2FsdA', hash_b64u: 'aGFzaA', set_at_ms: 7 },
      },
    ]);
    const got: string[] = [];
    b.onEvent((e, d) => got.push(`${e}:${JSON.stringify(d)}`));
    wv.emit({ rd: 1, kind: 'event', event: 'update_status', data: { state: 'DOWNLOADING' } });
    wv.emit(JSON.stringify({ rd: 1, kind: 'event', event: 'power_resumed', data: {} }));
    wv.emit({ rd: 2, kind: 'event', event: 'ignorado' });
    expect(got).toEqual(['update_status:{"state":"DOWNLOADING"}', 'power_resumed:{}']);
  });

  it('createBridge: RD_NATIVE + chrome.webview ⇒ nativa; senão navegador', () => {
    const g = { RD_NATIVE: native, chrome: { webview: new FakeWebview() } } as unknown as typeof globalThis;
    expect(createBridge(g)).toBeInstanceOf(NativeBridge);
    expect(createBridge({} as typeof globalThis)).toBeInstanceOf(WebBridge);
    expect(createBridge({ RD_NATIVE: native } as unknown as typeof globalThis)).toBeInstanceOf(WebBridge);
  });
});

describe('configuração sem domínio hardcoded', () => {
  const loc = { href: 'http://localhost:8088/', origin: 'http://localhost:8088' };
  const noFetch = (async () => new Response('', { status: 404 })) as typeof fetch;

  it('padrão: window.location (proxy same-origin)', async () => {
    const c = await loadConfig({ location: loc, fetch: noFetch, bridge: new WebBridge(), appVersion: '1.0.0' });
    expect(c).toMatchObject({ apiBase: 'http://localhost:8088', wsBase: 'ws://localhost:8088', source: 'location', platform: 'web' });
  });

  it('config.json servido junto ao app', async () => {
    const f = (async () => Response.json({ api_base_url: 'https://api.rd.example/', panel_url: 'https://painel.rd.example' })) as typeof fetch;
    const c = await loadConfig({ location: loc, fetch: f, bridge: new WebBridge(), appVersion: '1.0.0' });
    expect(c).toMatchObject({ apiBase: 'https://api.rd.example', wsBase: 'wss://api.rd.example', panelUrl: 'https://painel.rd.example', source: 'config.json' });
  });

  it('RD_NATIVE.config tem prioridade; config_ok=false é ignorado', async () => {
    const wv = { postMessage() {}, addEventListener() {} };
    const nb = new NativeBridge({ bridge: 'rd-native/1', platform: 'windows', shell_version: '2.0.0', config: { api_base_url: 'https://api.w', ws_base_url: 'wss://ws.w', config_ok: true } }, wv);
    const c = await loadConfig({ location: { href: 'https://player.rd.local/', origin: 'https://player.rd.local' }, fetch: noFetch, bridge: nb, appVersion: '1.0.0' });
    expect(c).toMatchObject({ apiBase: 'https://api.w', wsBase: 'wss://ws.w', platform: 'windows', appVersion: '2.0.0', source: 'native' });
    const bad = new NativeBridge({ bridge: 'rd-native/1', platform: 'windows', config: { api_base_url: 'https://api.w', config_ok: false } }, wv);
    const c2 = await loadConfig({ location: loc, fetch: noFetch, bridge: bad, appVersion: '1.0.0' });
    expect(c2.source).toBe('location');
    expect(deriveWsBase('https://x.y:8443')).toBe('wss://x.y:8443');
  });
});

describe('restrições de arquitetura (estáticas)', () => {
  const read = (dir: string) => readdirSync(dir).filter((f) => f.endsWith('.ts')).map((f) => [f, readFileSync(join(dir, f), 'utf8')] as const);
  const strip = (src: string) => src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');

  it('núcleo sem DOM (testável em Node): nada de document/window/navigator/localStorage', () => {
    for (const [f, src] of read(join(__dirname, '../src/core'))) {
      expect(strip(src), f).not.toMatch(/\b(document|window|navigator|localStorage|sessionStorage|HTMLElement)\b/);
    }
  });

  it('localStorage proibido em todo o app; sem innerHTML/eval', () => {
    const dirs = ['../src', '../src/core', '../src/adapters/web', '../src/ui'].map((d) => join(__dirname, d));
    for (const d of dirs) {
      for (const [f, src] of read(d)) {
        const code = strip(src);
        expect(code, f).not.toMatch(/localStorage|sessionStorage/);
        expect(code, f).not.toMatch(/\.innerHTML|outerHTML|insertAdjacentHTML|\beval\(|new Function\(/);
      }
    }
  });

  it('SHA-256 de mídia nunca por subtle.digest do arquivo inteiro', () => {
    for (const [f, src] of read(join(__dirname, '../src/core'))) expect(strip(src), f).not.toMatch(/subtle\.digest/);
  });
});
