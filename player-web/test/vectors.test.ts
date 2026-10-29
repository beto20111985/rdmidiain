/**
 * Conformidade com os vetores do backend (RDPP §9) — `protocol/vectors/*.json` (implementação de referência Python).
 * Se um arquivo não existir, o bloco correspondente fica PENDENTE (skip explícito) — nunca aprovado de mentira.
 * Os valores esperados NUNCA são ajustados aqui: divergência = defeito do player ou do vetor (reportar o caso).
 */

import { describe, expect, it } from 'vitest';
import { canonicalize, CanonicalError } from '../src/core/canonical';
import { canonicalSha256Hex, generateEs256KeyPair, importPkcs8Signer, importSpkiVerifier, signCanonical, verifyCanonical, WebCryptoSigner } from '../src/core/crypto';
import { resolve, type LocalState, type Segment } from '../src/core/cycle';
import { MemoryStateStore } from '../src/core/memoryStores';
import { PopRecorder } from '../src/core/pop';
import { validateManifest, ZERO_HASH, type Manifest } from '../src/core/protocol';
import { hasVector, loadVector, reviveFloats, VECTORS_DIR } from './helpers/vectors';

interface CanonCase {
  name: string;
  input: unknown;
  canonical?: string;
  error?: string;
}
interface SigCase {
  name: string;
  message: unknown;
  canonical: string;
  signature_b64u: string;
  public_spki_b64u: string;
  valid: boolean;
}
interface KeyPair {
  private_pkcs8_b64u: string;
  public_spki_b64u: string;
}
interface CycleCase {
  name: string;
  manifest: Manifest;
  anchor_ms: number;
  local_state: LocalState;
  queries: { t_ms: number; expected: Segment }[];
}
type Ev = Record<string, unknown> & { prev_hash?: string; seq?: number; playback_uuid?: string };
interface PopCase {
  name: string;
  event: Ev;
  canonical: string;
  signature_b64u: string;
  context: { last_hash: string | null; last_seq: number | null };
  expected: { status: string; valid: boolean; reason: string | null; flags?: string[] };
}
interface BatchCase {
  name: string;
  items: { event: Ev; signature: string }[];
  expected: { results: { playback_uuid: string; status: string; valid: boolean; reason: string | null }[]; ack_upto_seq: number | null };
}

const pending = (file: string) => `PENDENTE — ${file} ainda não publicado pelo backend em ${VECTORS_DIR}`;
const POP_KEYS = [
  'schema', 'playback_uuid', 'device_id', 'screen_id', 'creative_id', 'creative_code', 'campaign_id', 'insertion_id', 'kind',
  'manifest_version', 'started_at_ms', 'finished_at_ms', 'expected_ms', 'actual_ms', 'result', 'seq', 'nonce', 'prev_hash',
  'clock_offset_ms', 'offline',
].sort();

describe('carregador de vetores', () => {
  it('converte {"$float":…} em float real (ou JsonFloat se integral) e a canonicalização rejeita', () => {
    const v = reviveFloats({ a: { $float: '1.5' }, b: [{ $float: '2.0' }], c: { $float: 'x', y: 1 } }) as Record<string, unknown>;
    expect(v.a).toBe(1.5);
    expect(() => canonicalize(v.a)).toThrowError(/FLOAT_NOT_ALLOWED/);
    expect(() => canonicalize(v.b)).toThrowError(/FLOAT_NOT_ALLOWED/);
    expect(v.c).toEqual({ $float: 'x', y: 1 });
  });
});

describe.skipIf(!hasVector('canonical_json.json'))('vetores: canonical_json.json', () => {
  it(hasVector('canonical_json.json') ? 'todos os casos (texto canônico e rejeição de float)' : pending('canonical_json.json'), () => {
    const f = loadVector<CanonCase>('canonical_json.json', { revive: true });
    let ok = 0;
    let errors = 0;
    for (const c of f.cases) {
      if (c.error) {
        let err: unknown = null;
        try {
          canonicalize(c.input);
        } catch (e) {
          err = e;
        }
        expect(err, c.name).toBeInstanceOf(CanonicalError);
        expect((err as CanonicalError).code, c.name).toBe(c.error);
        errors++;
      } else {
        expect(canonicalize(c.input), c.name).toBe(c.canonical);
        ok++;
      }
    }
    expect(ok).toBeGreaterThan(0);
    expect(errors).toBeGreaterThan(0);
    console.info(`[vetores] canonical_json: ${ok} textos + ${errors} rejeições de float conferidos`);
  });
});

describe.skipIf(!hasVector('es256_signatures.json'))('vetores: es256_signatures.json', () => {
  it(hasVector('es256_signatures.json') ? 'verifica cada assinatura dada (raw r‖s, sem padding; DER/padding rejeitados; high-S aceito)' : pending('es256_signatures.json'), async () => {
    const f = loadVector<SigCase>('es256_signatures.json');
    const results: Record<string, boolean> = {};
    for (const c of f.cases) {
      expect(canonicalize(c.message), c.name).toBe(c.canonical);
      const verifier = await importSpkiVerifier(c.public_spki_b64u);
      const got = await verifyCanonical(verifier, c.message, c.signature_b64u);
      results[c.name] = got;
      expect(got, c.name).toBe(c.valid);
    }
    // Casos normativos explícitos (§0 atualizado), se presentes no arquivo.
    const expectIf = (name: string, v: boolean) => {
      if (name in results) expect(results[name], name).toBe(v);
    };
    expectIf('high_s_maleavel_aceita', true);
    expectIf('formato_der_nao_aceito', false);
    expectIf('base64url_com_padding_rejeitado', false);
    expectIf('truncada_63_bytes', false);
    expectIf('s_zero', false);
    expectIf('r_maior_ou_igual_n', false);
    console.info(`[vetores] es256_signatures: ${f.cases.length} assinaturas conferidas`);
  });

  it(hasVector('es256_signatures.json') ? 'round-trip próprio (assinar + verificar) com as chaves de teste' : pending('es256_signatures.json'), async () => {
    const f = loadVector<SigCase>('es256_signatures.json');
    const keys = [f.test_key, f.other_key].filter(Boolean) as KeyPair[];
    expect(keys.length).toBeGreaterThan(0);
    for (const key of keys) {
      const signer = await importPkcs8Signer(key.private_pkcs8_b64u, key.public_spki_b64u);
      const verifier = await importSpkiVerifier(key.public_spki_b64u);
      for (const c of f.cases.filter((x) => x.valid)) {
        const sig = await signCanonical(signer, c.message);
        expect(sig, c.name).toMatch(/^[A-Za-z0-9_-]{86}$/); // 64 bytes, base64url sem padding
        expect(await verifyCanonical(verifier, c.message, sig), `round-trip ${c.name}`).toBe(true);
      }
    }
  });
});

describe.skipIf(!hasVector('cycle_resolution.json'))('vetores: cycle_resolution.json', () => {
  it(hasVector('cycle_resolution.json') ? 'resolve() bate com a implementação de referência em todas as consultas' : pending('cycle_resolution.json'), () => {
    const f = loadVector<CycleCase>('cycle_resolution.json');
    let n = 0;
    const failures: string[] = [];
    for (const c of f.cases) {
      validateManifest(c.manifest);
      const snapshot = JSON.stringify(c.manifest);
      for (const q of c.queries) {
        const got = resolve(c.manifest, c.anchor_ms, c.local_state, q.t_ms);
        if (JSON.stringify(got) !== JSON.stringify({ block: q.expected.block, kind: q.expected.kind, creative_key: q.expected.creative_key, start_ms: q.expected.start_ms, end_ms: q.expected.end_ms })) {
          failures.push(`${c.name} @ ${q.t_ms}: esperado ${JSON.stringify(q.expected)} obtido ${JSON.stringify(got)}`);
        }
        n++;
      }
      expect(JSON.stringify(c.manifest), `${c.name}: resolve() não pode mutar o manifesto`).toBe(snapshot);
    }
    expect(failures, failures.join('\n')).toEqual([]);
    expect(n).toBeGreaterThan(0);
    console.info(`[vetores] cycle_resolution: ${f.cases.length} casos, ${n} consultas conferidas`);
  });
});

describe.skipIf(!hasVector('pop_validation.json'))('vetores: pop_validation.json (forma)', () => {
  it(hasVector('pop_validation.json') ? 'RDJ1 de cada evento, assinatura do dispositivo e encadeamento prev_hash' : pending('pop_validation.json'), async () => {
    const f = loadVector<PopCase>('pop_validation.json');
    const dk = f.device_key as KeyPair;
    const verifier = await importSpkiVerifier(dk.public_spki_b64u);
    let sigChecked = 0;
    let chainChecked = 0;
    for (const c of f.cases) {
      expect(canonicalize(c.event), c.name).toBe(c.canonical);
      const sigOk = await verifyCanonical(verifier, c.event, c.signature_b64u);
      if (c.expected.reason === 'BAD_SIGNATURE') expect(sigOk, c.name).toBe(false);
      if (c.expected.valid) {
        expect(sigOk, c.name).toBe(true);
        expect(Object.keys(c.event).sort(), c.name).toEqual(POP_KEYS);
        sigChecked++;
      }
      const flags = c.expected.flags ?? [];
      if (c.expected.reason === null && !flags.includes('CHAIN_BREAK') && typeof c.event.prev_hash === 'string') {
        expect(c.event.prev_hash, `${c.name}: prev_hash`).toBe(c.context.last_hash ?? ZERO_HASH);
        chainChecked++;
      }
    }
    expect(sigChecked).toBeGreaterThan(0);
    console.info(`[vetores] pop_validation: ${f.cases.length} eventos (RDJ1), ${sigChecked} assinaturas válidas, ${chainChecked} prev_hash conferidos`);
  });

  it(hasVector('pop_validation.json') ? 'lote: prev_hash = sha256(RDJ1(evento anterior)) e assinaturas coerentes com o resultado' : pending('pop_validation.json'), async () => {
    const f = loadVector<PopCase>('pop_validation.json');
    const batches = (f.batch_cases ?? []) as BatchCase[];
    const dk = f.device_key as KeyPair;
    const verifier = await importSpkiVerifier(dk.public_spki_b64u);
    let links = 0;
    for (const b of batches) {
      for (const [i, it] of b.items.entries()) {
        const reason = b.expected.results[i]?.reason ?? null;
        if (reason === 'SCHEMA_INVALID') continue;
        const ok = await verifyCanonical(verifier, it.event, it.signature);
        if (reason === 'BAD_SIGNATURE') expect(ok, `${b.name}[${i}]`).toBe(false);
        else expect(ok, `${b.name}[${i}]`).toBe(true);
      }
      const bySeq = new Map<number, Ev>();
      for (const it of b.items) if (typeof it.event.seq === 'number' && !bySeq.has(it.event.seq)) bySeq.set(it.event.seq, it.event);
      for (const [seq, ev] of bySeq) {
        const prev = bySeq.get(seq - 1);
        if (prev && typeof ev.prev_hash === 'string' && ev.prev_hash === canonicalSha256Hex(prev)) links++;
      }
    }
    if (batches.length) expect(links, 'ao menos um elo prev_hash do lote reproduzido pelo player').toBeGreaterThan(0);
    console.info(`[vetores] pop_validation: ${batches.length} lote(s), ${links} elo(s) prev_hash reproduzidos`);
  });

  it(hasVector('pop_validation.json') ? 'evento gerado pelo PopRecorder tem a mesma forma (chaves e tipos) do evento válido do vetor' : pending('pop_validation.json'), async () => {
    const f = loadVector<PopCase>('pop_validation.json');
    const ref = f.cases.find((c) => c.expected.valid && c.event.campaign_id !== null && c.event.insertion_id !== null)!.event;
    const signer = new WebCryptoSigner(await generateEs256KeyPair(false));
    const rec = new PopRecorder({ store: new MemoryStateStore(), signer: () => signer, identity: () => ({ device_id: String(ref.device_id), screen_id: String(ref.screen_id) }) });
    const mine = (await rec.record({
      creative: { creative_id: String(ref.creative_id), code: String(ref.creative_code), campaign_id: String(ref.campaign_id), kind: 'EXTERNAL' },
      insertion_id: String(ref.insertion_id),
      manifest_version: Number(ref.manifest_version),
      started_at_ms: Number(ref.started_at_ms),
      expected_ms: Number(ref.expected_ms),
      actual_ms: Number(ref.actual_ms),
      result: 'COMPLETED',
      clock_offset_ms: 0,
      offline: false,
    }))!.event as unknown as Record<string, unknown>;
    expect(Object.keys(mine).sort()).toEqual(Object.keys(ref).sort());
    for (const k of Object.keys(ref)) expect(typeof mine[k], k).toBe(typeof ref[k]);
    expect(mine.finished_at_ms).toBe((mine.started_at_ms as number) + (mine.actual_ms as number));
  });
});
