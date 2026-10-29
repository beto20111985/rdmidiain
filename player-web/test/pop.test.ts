import { describe, expect, it } from 'vitest';
import { canonicalSha256Hex, generateEs256KeyPair, importSpkiVerifier, verifyCanonical, WebCryptoSigner, type Signer } from '../src/core/crypto';
import { MemoryStateStore } from '../src/core/memoryStores';
import { decideResult, measureActualMs, PopRecorder, type PopInput } from '../src/core/pop';
import { ZERO_HASH, type BatchResponse, type SignedPopEvent } from '../src/core/protocol';
import { b64uDecode } from '../src/core/util';

const creative = { creative_id: 'cr-1', code: 'RD-A7M2P9', campaign_id: 'camp-1', kind: 'EXTERNAL' as const };

function input(over: Partial<PopInput> = {}): PopInput {
  return {
    creative,
    insertion_id: 'ins-1',
    manifest_version: 42,
    started_at_ms: 1_791_380_000_000,
    expected_ms: 30_000,
    actual_ms: 30_000,
    result: 'COMPLETED',
    clock_offset_ms: -120,
    offline: false,
    ...over,
  };
}

async function setup(store = new MemoryStateStore(), signer?: Signer, deviceId = 'dev-1') {
  const s = signer ?? new WebCryptoSigner(await generateEs256KeyPair(false));
  const rec = new PopRecorder({ store, signer: () => s, identity: () => ({ device_id: deviceId, screen_id: 'scr-1' }) });
  return { store, signer: s, rec };
}

describe('regra de 100 % (sem tolerância)', () => {
  const base = { expectedMs: 30_000, isVideo: false, videoEnded: false, error: false, interrupted: false };

  it('30/30 é COMPLETED', () => {
    expect(decideResult({ ...base, actualMs: 30_000 })).toBe('COMPLETED');
    expect(decideResult({ ...base, actualMs: 30_250 })).toBe('COMPLETED');
  });

  it('29,9/30 e 29 999/30 000 NÃO são COMPLETED', () => {
    expect(decideResult({ ...base, actualMs: 29_900 })).toBe('INTERRUPTED');
    expect(decideResult({ ...base, actualMs: 29_999 })).toBe('INTERRUPTED');
    expect(decideResult({ ...base, actualMs: 25_000 })).toBe('INTERRUPTED');
  });

  it('medição monotônica arredonda para baixo (29 999,9 ms ⇒ 29 999)', () => {
    expect(measureActualMs(1000.2, 31000.1)).toBe(29_999);
    expect(decideResult({ ...base, actualMs: measureActualMs(1000.2, 31000.1) })).toBe('INTERRUPTED');
    expect(measureActualMs(5, 2)).toBe(0);
  });

  it('vídeo exige o evento de fim do decodificador', () => {
    expect(decideResult({ ...base, isVideo: true, actualMs: 30_000, videoEnded: false })).toBe('INTERRUPTED');
    expect(decideResult({ ...base, isVideo: true, actualMs: 30_000, videoEnded: true })).toBe('COMPLETED');
  });

  it('erro ⇒ ERROR; interrupção/página oculta ⇒ INTERRUPTED mesmo com tempo cheio', () => {
    expect(decideResult({ ...base, actualMs: 30_000, error: true })).toBe('ERROR');
    expect(decideResult({ ...base, actualMs: 30_000, interrupted: true })).toBe('INTERRUPTED');
  });
});

describe('PopRecorder — evento assinado, seq, nonce, prev_hash', () => {
  it('evento tem exatamente as chaves do RDPP §7 e assinatura ES256 válida sobre o RDJ1', async () => {
    const { rec, signer } = await setup();
    const signed = (await rec.record(input()))!;
    expect(Object.keys(signed.event).sort()).toEqual(
      [
        'schema', 'playback_uuid', 'device_id', 'screen_id', 'creative_id', 'creative_code', 'campaign_id', 'insertion_id', 'kind',
        'manifest_version', 'started_at_ms', 'finished_at_ms', 'expected_ms', 'actual_ms', 'result', 'seq', 'nonce', 'prev_hash',
        'clock_offset_ms', 'offline',
      ].sort(),
    );
    const e = signed.event;
    expect(e.schema).toBe('rd.pop/1');
    expect(e.playback_uuid).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(b64uDecode(e.nonce)).toHaveLength(16);
    expect(e.prev_hash).toBe(ZERO_HASH);
    expect(e.seq).toBe(1);
    expect(e.finished_at_ms).toBe(e.started_at_ms + e.actual_ms);
    const verifier = await importSpkiVerifier(await signer.publicKeySpki());
    expect(await verifyCanonical(verifier, e, signed.signature)).toBe(true);
    expect(await verifyCanonical(verifier, { ...e, actual_ms: 30_001 }, signed.signature)).toBe(false);
  });

  it('30/30 válido localmente é gravado COMPLETED; 29,9/30 fica INTERRUPTED', async () => {
    const { rec } = await setup();
    const ok = await rec.record(input({ actual_ms: 30_000, result: decideResult({ actualMs: 30_000, expectedMs: 30_000, isVideo: false, videoEnded: false, error: false, interrupted: false }) }));
    const bad = await rec.record(
      input({ actual_ms: 29_900, result: decideResult({ actualMs: 29_900, expectedMs: 30_000, isVideo: false, videoEnded: false, error: false, interrupted: false }) }),
    );
    expect(ok!.event.result).toBe('COMPLETED');
    expect(bad!.event.result).toBe('INTERRUPTED');
  });

  it('prev_hash encadeia: sha256 do RDJ1 do evento anterior', async () => {
    const { rec } = await setup();
    const a = (await rec.record(input()))!;
    const b = (await rec.record(input({ started_at_ms: a.event.finished_at_ms })))!;
    const c = (await rec.record(input({ started_at_ms: b.event.finished_at_ms })))!;
    expect(b.event.prev_hash).toBe(canonicalSha256Hex(a.event));
    expect(c.event.prev_hash).toBe(canonicalSha256Hex(b.event));
    expect([a.event.seq, b.event.seq, c.event.seq]).toEqual([1, 2, 3]);
    expect(new Set([a.event.nonce, b.event.nonce, c.event.nonce]).size).toBe(3);
  });

  it('seq e cadeia persistem entre "reboots" (nova instância, mesmo armazenamento)', async () => {
    const store = new MemoryStateStore();
    const signer = new WebCryptoSigner(await generateEs256KeyPair(false));
    let last: SignedPopEvent | null = null;
    for (let boot = 0; boot < 3; boot++) {
      const { rec } = await setup(store, signer);
      for (let i = 0; i < 4; i++) last = await rec.record(input({ started_at_ms: 1_791_380_000_000 + (boot * 4 + i) * 30_000 }));
    }
    expect(last!.event.seq).toBe(12);
    const all = await store.outboxList(100);
    expect(all.map((r) => r.seq)).toEqual(Array.from({ length: 12 }, (_, i) => i + 1));
    for (let i = 1; i < all.length; i++) expect(all[i]!.event.prev_hash).toBe(canonicalSha256Hex(all[i - 1]!.event));
  });

  it('chamadas concorrentes são serializadas (sem seq duplicado)', async () => {
    const { rec } = await setup();
    const evs = await Promise.all(Array.from({ length: 25 }, (_, i) => rec.record(input({ started_at_ms: 1_791_380_000_000 + i * 30_000 }))));
    expect(evs.map((e) => e!.event.seq).sort((a, b) => a - b)).toEqual(Array.from({ length: 25 }, (_, i) => i + 1));
  });

  it('falha na transação não avança a cadeia', async () => {
    const { rec, store } = await setup();
    await rec.record(input());
    store.failNextTransact = new Error('disco');
    await expect(rec.record(input({ started_at_ms: 1_791_380_030_000 }))).rejects.toThrow('disco');
    const next = await rec.record(input({ started_at_ms: 1_791_380_060_000 }));
    expect(next!.event.seq).toBe(2);
  });

  it('SKIPPED: started = finished, actual 0, expected = d do item', async () => {
    const { rec } = await setup();
    const s = (await rec.skipped({ ...input(), expected_ms: 30_000 }))!;
    expect(s.event.result).toBe('SKIPPED');
    expect(s.event.actual_ms).toBe(0);
    expect(s.event.finished_at_ms).toBe(s.event.started_at_ms);
    expect(s.event.expected_ms).toBe(30_000);
  });

  it('salvaguarda de sobreposição: começa no fim do anterior, desconta do próprio item (sem propagar) e deixa de ser COMPLETED', async () => {
    const { rec } = await setup();
    const a = (await rec.record(input({ started_at_ms: 1_000_000, actual_ms: 30_000 })))!;
    const b = (await rec.record(input({ started_at_ms: 1_029_990, actual_ms: 30_000 })))!;
    expect(b.event.started_at_ms).toBe(a.event.finished_at_ms);
    expect(b.event.actual_ms).toBe(29_990);
    expect(b.event.finished_at_ms).toBe(1_059_990); // o fim não se move
    expect(b.event.result).toBe('INTERRUPTED');
    const c = (await rec.record(input({ started_at_ms: 1_059_990, actual_ms: 30_000 })))!;
    expect(c.event.started_at_ms).toBe(1_059_990); // sem propagação
    expect(c.event.result).toBe('COMPLETED');
  });

  it('dispositivo novo (re-pareamento) recomeça a cadeia', async () => {
    const store = new MemoryStateStore();
    const signer = new WebCryptoSigner(await generateEs256KeyPair(false));
    const r1 = (await setup(store, signer, 'dev-1')).rec;
    await r1.record(input());
    await r1.record(input({ started_at_ms: 1_791_380_030_000 }));
    const r2 = (await setup(store, signer, 'dev-2')).rec;
    const e = (await r2.record(input({ started_at_ms: 1_791_380_060_000 })))!;
    expect(e.event.seq).toBe(1);
    expect(e.event.prev_hash).toBe(ZERO_HASH);
  });

  it('sem identidade (não pareado) não grava', async () => {
    const store = new MemoryStateStore();
    const rec = new PopRecorder({ store, signer: () => null, identity: () => null });
    expect(await rec.record(input())).toBeNull();
    expect(await store.outboxCount()).toBe(0);
  });
});

describe('outbox — envio em lote e remoção só após ack', () => {
  async function filled(n: number) {
    const ctx = await setup();
    for (let i = 0; i < n; i++) await ctx.rec.record(input({ started_at_ms: 1_791_380_000_000 + i * 30_000 }));
    return ctx;
  }

  it('lotes ≤ 200, em ordem de seq; remove os confirmados (ACCEPTED/DUPLICATE/REJECTED)', async () => {
    const { rec, store } = await filled(450);
    const sizes: number[] = [];
    const statuses = ['ACCEPTED', 'DUPLICATE', 'REJECTED'] as const;
    const res = await rec.flush(async (events) => {
      sizes.push(events.length);
      const seqs = events.map((e) => e.event.seq);
      expect(seqs).toEqual([...seqs].sort((a, b) => a - b));
      return {
        results: events.map((e, i) => ({ playback_uuid: e.event.playback_uuid, status: statuses[i % 3]!, valid: i % 3 === 0, reason: null })),
        ack_upto_seq: seqs[seqs.length - 1]!,
      };
    });
    expect(sizes).toEqual([200, 200, 50]);
    expect(res.removed).toBe(450);
    expect(await store.outboxCount()).toBe(0);
  });

  it('lote parcial: só remove o que foi confirmado e para; o resto vai no próximo flush', async () => {
    const { rec, store } = await filled(10);
    let calls = 0;
    const res = await rec.flush(async (events): Promise<BatchResponse> => {
      calls++;
      return {
        results: events.slice(0, 4).map((e) => ({ playback_uuid: e.event.playback_uuid, status: 'ACCEPTED', valid: true, reason: null })),
        ack_upto_seq: 4,
      };
    });
    expect(calls).toBe(1);
    expect(res.removed).toBe(4);
    expect(res.remaining).toBe(6);
    expect((await store.outboxList(100)).map((r) => r.seq)).toEqual([5, 6, 7, 8, 9, 10]);
  });

  it('falha de rede não remove nada', async () => {
    const { rec, store } = await filled(5);
    await expect(rec.flush(async () => Promise.reject(new Error('offline')))).rejects.toThrow('offline');
    expect(await store.outboxCount()).toBe(5);
  });

  it('status não definitivo/ausente mantém o evento', async () => {
    const { rec, store } = await filled(3);
    await rec.flush(async (events) => ({
      results: [{ playback_uuid: events[0]!.event.playback_uuid, status: 'PENDING' as never, valid: false, reason: null }],
      ack_upto_seq: null,
    }));
    expect(await store.outboxCount()).toBe(3);
  });

  it('eventos de um device_id anterior são descartados (não podem mais ser entregues)', async () => {
    const store = new MemoryStateStore();
    const signer = new WebCryptoSigner(await generateEs256KeyPair(false));
    const old = (await setup(store, signer, 'dev-old')).rec;
    await old.record(input());
    const cur = (await setup(store, signer, 'dev-new')).rec;
    await cur.record(input({ started_at_ms: 1_791_380_030_000 }));
    const seen: string[] = [];
    const r = await cur.flush(async (events) => {
      events.forEach((e) => seen.push(e.event.device_id));
      return { results: events.map((e) => ({ playback_uuid: e.event.playback_uuid, status: 'ACCEPTED' as const, valid: true, reason: null })), ack_upto_seq: 1 };
    });
    expect(seen).toEqual(['dev-new']);
    expect(r.discardedForeign).toBe(1);
    expect(await store.outboxCount()).toBe(0);
  });
});
