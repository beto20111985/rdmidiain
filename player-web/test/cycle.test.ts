import { describe, expect, it } from 'vitest';
import { mapBucketToHorizon, resolve, type LocalState, type Segment } from '../src/core/cycle';
import type { Manifest } from '../src/core/protocol';
import { ALL_MEDIA, ANCHOR_0807 as A, creative, DAY, manaus, media, MIN, mk, SEC, WEEK } from './helpers/manifest';

const C = 4 * MIN;
const I = 2 * MIN;
const ready: LocalState = { revoked: [], media_ready: ALL_MEDIA };

function at(m: Manifest, t: number, local: LocalState = ready, anchor = A): Segment {
  return resolve(m, anchor, local, t);
}

function seg(block: Segment['block'], kind: Segment['kind'], key: string | null, start: number, end: number): Segment {
  return { block, kind, creative_key: key, start_ms: start, end_ms: end };
}

describe('ciclo ancorado — exemplos do A6 §10.6 (âncora 08:07, política 2+2)', () => {
  const m = mk();

  it('reboot às 08:47:00 ⇒ ciclo #10, bloco interno, offset 0 (âncora mantida)', () => {
    expect(at(m, manaus(8, 47))).toEqual(seg('INTERNAL', 'ITEM', 'c7', manaus(8, 47), manaus(8, 48)));
  });

  it('reboot às 08:49:10 ⇒ ciclo #10, bloco externo; resolve devolve o ITEM iniciado às 08:49:00 (meio de item é regra do runtime)', () => {
    expect(at(m, manaus(8, 49, 10))).toEqual(seg('EXTERNAL', 'ITEM', 'c1', manaus(8, 49), manaus(8, 49, 30)));
    // próxima fronteira de item (08:49:30) é o segundo anúncio
    expect(at(m, manaus(8, 49, 30))).toEqual(seg('EXTERNAL', 'ITEM', 'c2', manaus(8, 49, 30), manaus(8, 50)));
  });

  it('09:07:00 ⇒ ciclo #15 (ciclo de 1 h preservado: 15 ciclos de 4 min)', () => {
    expect(at(m, manaus(9, 7))).toEqual(seg('INTERNAL', 'ITEM', 'c7', manaus(9, 7), manaus(9, 8)));
    expect(at(m, manaus(9, 6, 59, 999)).block).toBe('EXTERNAL');
  });

  it('unidades externas sem entrada ⇒ fallback em unidades de 30 s', () => {
    expect(at(m, manaus(8, 50, 10))).toEqual(seg('EXTERNAL', 'ITEM', 'f1', manaus(8, 50), manaus(8, 50, 30)));
    expect(at(m, manaus(8, 50, 45))).toEqual(seg('EXTERNAL', 'ITEM', 'f1', manaus(8, 50, 30), manaus(8, 51)));
  });

  it('é determinístico e independente da ordem/instante de consulta (reboot não recalcula)', () => {
    const ts = [manaus(8, 49, 10), manaus(8, 47), manaus(9, 7), manaus(8, 50, 45)];
    const first = ts.map((t) => at(m, t));
    const again = [...ts].reverse().map((t) => at(m, t)).reverse();
    expect(again).toEqual(first);
  });

  it('instante anterior à âncora usa k negativo com módulo positivo', () => {
    expect(at(m, A - 1)).toEqual(seg('EXTERNAL', 'ITEM', 'f1', A - 30 * SEC, A));
    expect(at(m, A - C)).toEqual(seg('INTERNAL', 'ITEM', 'c7', A - C, A - C + MIN));
  });
});

describe('políticas de rotação', () => {
  it('3+1: bloco interno de 3 min com padrão de 2 min ⇒ resto do bloco em fallback; externo de 1 min', () => {
    const m = mk({ internalMs: 3 * MIN, externalMs: 1 * MIN });
    const k10 = A + 10 * C;
    expect(at(m, k10 + 61 * SEC)).toEqual(seg('INTERNAL', 'ITEM', 'c8', k10 + MIN, k10 + 2 * MIN));
    expect(at(m, k10 + 155 * SEC)).toEqual(seg('INTERNAL', 'ITEM', 'f1', k10 + 150 * SEC, k10 + 180 * SEC));
    const s = k10 + 3 * MIN;
    expect(at(m, s + 5 * SEC)).toEqual(seg('EXTERNAL', 'ITEM', 'c1', s, s + 30 * SEC));
    expect(at(m, s + 35 * SEC)).toEqual(seg('EXTERNAL', 'ITEM', 'c2', s + 30 * SEC, s + 60 * SEC));
  });

  it('2+2 com anúncio de 120 s ocupa o bloco externo inteiro', () => {
    const base = mk();
    const m = mk({
      creatives: { ...base.creatives, g2: creative('g2gggg', 'EXTERNAL', 'mg', { duration_ms: 120_000 }) },
      entries: (b) => [{ i: `ins-${b}`, c: 'g2', u: 0, d: 120_000 }],
    });
    const s = A + 3 * C + I;
    expect(at(m, s + 100 * SEC)).toEqual(seg('EXTERNAL', 'ITEM', 'g2', s, s + 2 * MIN));
  });

  it('rotação do padrão interno entre ciclos (pattern[k mod len])', () => {
    const m = mk({ internalPattern: [[{ c: 'c7', d: 120_000 }], [{ c: 'c8', d: 120_000 }]] });
    expect(at(m, A + 10 * C).creative_key).toBe('c7');
    expect(at(m, A + 11 * C).creative_key).toBe('c8');
    expect(at(m, A - 1 * C).creative_key).toBe('c8'); // k = −1 ⇒ índice 1
  });

  it('padrão interno vazio ⇒ bloco interno inteiro em fallback', () => {
    const m = mk({ internalPattern: [] });
    expect(at(m, A + 30 * SEC)).toEqual(seg('INTERNAL', 'ITEM', 'f1', A + 30 * SEC, A + 60 * SEC));
  });

  it('item interno que ultrapassa o fim do bloco não toca; o resto vai para fallback', () => {
    const m = mk({
      internalPattern: [
        [
          { c: 'c7', d: 60_000 },
          { c: 'g1', d: 90_000 },
        ],
      ],
    });
    expect(at(m, A + 70 * SEC)).toEqual(seg('INTERNAL', 'ITEM', 'f1', A + 60 * SEC, A + 90 * SEC));
    expect(at(m, A + 100 * SEC)).toEqual(seg('INTERNAL', 'ITEM', 'f1', A + 90 * SEC, A + 120 * SEC));
  });
});

describe('validade, revogação, kill e mídia', () => {
  const s = A + 10 * C + I; // início do bloco externo do ciclo 10 (08:49:00)

  it('validade checada no INÍCIO do item: válido no início ⇒ toca inteiro', () => {
    const base = mk();
    const m = mk({ creatives: { ...base.creatives, c1: { ...base.creatives.c1!, valid_until_ms: s + 10 * SEC } } });
    expect(at(m, s + 20 * SEC)).toEqual(seg('EXTERNAL', 'ITEM', 'c1', s, s + 30 * SEC));
  });

  it('item vencido (valid_until ≤ início) ⇒ intervalo vai para fallback (mesmo offline)', () => {
    const base = mk();
    const m = mk({ creatives: { ...base.creatives, c1: { ...base.creatives.c1!, valid_until_ms: s } } });
    expect(at(m, s + 5 * SEC)).toEqual(seg('EXTERNAL', 'ITEM', 'f1', s, s + 30 * SEC));
    // o seguinte (c2) continua
    expect(at(m, s + 35 * SEC).creative_key).toBe('c2');
  });

  it('item ainda não vigente (valid_from > início) ⇒ fallback', () => {
    const base = mk();
    const m = mk({ creatives: { ...base.creatives, c2: { ...base.creatives.c2!, valid_from_ms: s + 31 * SEC } } });
    expect(at(m, s + 35 * SEC)).toEqual(seg('EXTERNAL', 'ITEM', 'f1', s + 30 * SEC, s + 60 * SEC));
  });

  it('revogado no manifesto ⇒ fallback', () => {
    const m = mk({ revoked: ['cr-c1aaaa'] });
    expect(at(m, s + 5 * SEC)).toEqual(seg('EXTERNAL', 'ITEM', 'f1', s, s + 30 * SEC));
  });

  it('kill local (local_state.revoked) ⇒ fallback; união com manifest.revoked', () => {
    const m = mk({ revoked: ['cr-c2bbbb'] });
    const local: LocalState = { revoked: ['cr-c1aaaa'], media_ready: ALL_MEDIA };
    expect(at(m, s + 5 * SEC, local).creative_key).toBe('f1');
    expect(at(m, s + 35 * SEC, local).creative_key).toBe('f1');
  });

  it('mídia não pronta/sem sha verificado ⇒ fallback', () => {
    const local: LocalState = { revoked: [], media_ready: ALL_MEDIA.filter((k) => k !== 'm1') };
    expect(at(mk(), s + 5 * SEC, local)).toEqual(seg('EXTERNAL', 'ITEM', 'f1', s, s + 30 * SEC));
  });

  it('fallback NÃO é checado em resolve() (§8.2.1)', () => {
    const local: LocalState = { revoked: ['cr-f1eeee'], media_ready: ALL_MEDIA.filter((k) => k !== 'mf') };
    expect(at(mk(), s + 70 * SEC, local)).toEqual(seg('EXTERNAL', 'ITEM', 'f1', s + 60 * SEC, s + 90 * SEC));
  });
});

describe('fallback (§5.2)', () => {
  const s = A + 10 * C + I;

  it('ordem cíclica a partir de floor((s − A)/U) mod len', () => {
    const base = mk();
    const m = mk({
      creatives: { ...base.creatives, f2: creative('f2zzzz', 'FALLBACK', 'mf') },
      fallback: [
        { c: 'f1', d: 30_000 },
        { c: 'f2', d: 30_000 },
      ],
      entries: () => [],
    });
    // s − A = 10·C + I = 2 520 000 ⇒ unidade 84 ⇒ índice 0
    expect(at(m, s + 1).creative_key).toBe('f1');
    expect(at(m, s + 31 * SEC).creative_key).toBe('f2');
    expect(at(m, s + 61 * SEC).creative_key).toBe('f1');
  });

  it('fallback vazio ⇒ FILLER com creative_key null (tela institucional embutida)', () => {
    const m = mk({ fallback: [], entries: () => [] });
    expect(at(m, s + 5 * SEC)).toEqual(seg('EXTERNAL', 'FILLER', null, s, s + 2 * MIN));
  });

  it('resto menor que o próximo item ⇒ FILLER exibindo fallback[índice] (sem PoP)', () => {
    const base = mk();
    const m = mk({
      creatives: { ...base.creatives, f9: creative('f9yyyy', 'FALLBACK', 'mg', { duration_ms: 60_000 }) },
      fallback: [{ c: 'f9', d: 60_000 }],
      entries: (b) => [{ i: `i-${b}`, c: 'c1', u: 1, d: 30_000 }],
    });
    // lacuna [s, s+30) < 60 s ⇒ FILLER f9
    expect(at(m, s + 5 * SEC)).toEqual(seg('EXTERNAL', 'FILLER', 'f9', s, s + 30 * SEC));
    // depois do c1: lacuna [s+60, s+120) = 60 s ⇒ ITEM f9
    expect(at(m, s + 70 * SEC)).toEqual(seg('EXTERNAL', 'ITEM', 'f9', s + 60 * SEC, s + 120 * SEC));
  });
});

describe('defesa do external_plan (§8.2.4)', () => {
  const s = A + 10 * C + I;
  const end = s + 2 * MIN;
  const base = mk();
  const withF9 = { ...base.creatives, f9: creative('f9yyyy', 'FALLBACK', 'mg', { duration_ms: 60_000 }) };

  it('sobreposição: a primeira toca, a posterior não; resto vai para fallback', () => {
    const m = mk({
      entries: (b) => [
        { i: `a-${b}`, c: 'g1', u: 0, d: 60_000 },
        { i: `b-${b}`, c: 'c2', u: 1, d: 30_000 },
      ],
    });
    expect(at(m, s + 40 * SEC)).toEqual(seg('EXTERNAL', 'ITEM', 'g1', s, s + 60 * SEC));
    expect(at(m, s + 65 * SEC)).toEqual(seg('EXTERNAL', 'ITEM', 'f1', s + 60 * SEC, s + 90 * SEC));
  });

  it('entrada que ultrapassa o bloco: lacuna anterior e intervalo recortado são intervalos de fallback próprios (sem fusão)', () => {
    const m = mk({
      creatives: withF9,
      fallback: [{ c: 'f9', d: 60_000 }],
      entries: (b) => [{ i: `x-${b}`, c: 'g1', u: 3, d: 60_000 }],
    });
    // lacuna [s, s+90): f9 [s, s+60) + FILLER [s+60, s+90)
    expect(at(m, s + 10 * SEC)).toEqual(seg('EXTERNAL', 'ITEM', 'f9', s, s + 60 * SEC));
    expect(at(m, s + 70 * SEC)).toEqual(seg('EXTERNAL', 'FILLER', 'f9', s + 60 * SEC, s + 90 * SEC));
    // entrada rejeitada [s+90, fim) = 30 s ⇒ FILLER f9 (se fundisse, seria ITEM f9 [s+60, s+120))
    expect(at(m, s + 100 * SEC)).toEqual(seg('EXTERNAL', 'FILLER', 'f9', s + 90 * SEC, end));
  });

  it('entrada com início ≥ fim do bloco é ignorada', () => {
    const m = mk({ entries: (b) => [{ i: `z-${b}`, c: 'c1', u: 4, d: 30_000 }] });
    expect(at(m, end - 1)).toEqual(seg('EXTERNAL', 'ITEM', 'f1', end - 30 * SEC, end));
  });

  it('item inválido é intervalo próprio (não funde com a lacuna adjacente)', () => {
    const m = mk({
      creatives: withF9,
      fallback: [{ c: 'f9', d: 60_000 }],
      revoked: ['cr-c1aaaa'],
      entries: (b) => [{ i: `r-${b}`, c: 'c1', u: 1, d: 30_000 }],
    });
    // lacuna [s, s+30) ⇒ FILLER; c1 revogado [s+30, s+60) ⇒ FILLER; lacuna [s+60, s+120) ⇒ ITEM f9
    expect(at(m, s + 5 * SEC)).toEqual(seg('EXTERNAL', 'FILLER', 'f9', s, s + 30 * SEC));
    expect(at(m, s + 35 * SEC)).toEqual(seg('EXTERNAL', 'FILLER', 'f9', s + 30 * SEC, s + 60 * SEC));
    expect(at(m, s + 65 * SEC)).toEqual(seg('EXTERNAL', 'ITEM', 'f9', s + 60 * SEC, end));
  });
});

describe('horizonte e repetição semanal', () => {
  const special = (m: Manifest): number => Number(Object.keys(m.external_plan)[0]);

  it('fora do horizonte ⇒ b − n·7 dias (offline indefinido não para os anúncios)', () => {
    const probe = mk({ horizonDays: 1 });
    const firstBucketWithS = (() => {
      // bucket do bloco externo do ciclo 10
      const s = A + 10 * C + I;
      return Math.floor(s / C) * C;
    })();
    const m = mk({
      horizonDays: 1,
      entries: (b) => (b === firstBucketWithS ? [{ i: 'only', c: 'c1', u: 0, d: 30_000 }] : []),
    });
    expect(special(m)).toBe(firstBucketWithS);
    expect(probe.horizon.to_ms - probe.horizon.from_ms).toBe(DAY);
    const s = A + 10 * C + I;
    expect(at(m, s + 5 * SEC).creative_key).toBe('c1');
    expect(at(m, s + WEEK + 5 * SEC)).toEqual(seg('EXTERNAL', 'ITEM', 'c1', s + WEEK, s + WEEK + 30 * SEC));
    expect(at(m, s + 2 * WEEK + 5 * SEC).creative_key).toBe('c1');
    expect(at(m, s + 10 * WEEK + 5 * SEC).creative_key).toBe('c1');
  });

  it('antes do horizonte ⇒ b + n·7 dias', () => {
    const s = A + 10 * C + I;
    const b = Math.floor(s / C) * C;
    const m = mk({ horizonDays: 1, entries: (x) => (x === b ? [{ i: 'only', c: 'c1', u: 0, d: 30_000 }] : []) });
    expect(at(m, s - WEEK + 5 * SEC).creative_key).toBe('c1');
  });

  it('bucket mapeado que não cai no horizonte ⇒ bloco externo inteiro em fallback', () => {
    const m = mk({ horizonDays: 1 });
    expect(mapBucketToHorizon(m, m.horizon.from_ms + 3 * DAY)).toBeNull();
    const s = A + 10 * C + I + 3 * DAY;
    expect(at(m, s + 5 * SEC)).toEqual(seg('EXTERNAL', 'ITEM', 'f1', s, s + 30 * SEC));
  });

  it('validade checada contra o t real (não o b′): item vencido na semana repetida ⇒ fallback', () => {
    const s = A + 10 * C + I;
    const base = mk();
    const m = mk({
      creatives: { ...base.creatives, c1: { ...base.creatives.c1!, valid_until_ms: s + 2 * DAY } },
    });
    expect(at(m, s + 5 * SEC).creative_key).toBe('c1');
    expect(at(m, s + WEEK + 5 * SEC).creative_key).toBe('f1');
  });
});

describe('emergência', () => {
  const X = A + 10 * C + 15 * SEC; // começa no meio do bloco interno

  it('preempta tudo; repete em passos de duration_ms a partir de from', () => {
    const m = mk({ emergency: [{ id: 'e1', c: 'c1', from_ms: X, until_ms: X + 75 * SEC }] });
    expect(at(m, X)).toEqual(seg('EMERGENCY', 'ITEM', 'c1', X, X + 30 * SEC));
    expect(at(m, X + 40 * SEC)).toEqual(seg('EMERGENCY', 'ITEM', 'c1', X + 30 * SEC, X + 60 * SEC));
  });

  it('última repetição que não cabe ⇒ FILLER com o próprio criativo (block EMERGENCY)', () => {
    const m = mk({ emergency: [{ id: 'e1', c: 'c1', from_ms: X, until_ms: X + 75 * SEC }] });
    expect(at(m, X + 70 * SEC)).toEqual(seg('EMERGENCY', 'FILLER', 'c1', X + 60 * SEC, X + 75 * SEC));
  });

  it('após until_ms volta à grade normal; antes de from também', () => {
    const m = mk({ emergency: [{ id: 'e1', c: 'c1', from_ms: X, until_ms: X + 75 * SEC }] });
    expect(at(m, X + 75 * SEC)).toEqual(seg('INTERNAL', 'ITEM', 'c8', A + 10 * C + MIN, A + 10 * C + 2 * MIN));
    expect(at(m, X - 1)).toEqual(seg('INTERNAL', 'ITEM', 'c7', A + 10 * C, A + 10 * C + MIN));
  });

  it('vale a primeira entrada ativa da lista', () => {
    const m = mk({
      emergency: [
        { id: 'e1', c: 'c2', from_ms: X + 10 * SEC, until_ms: X + 20 * SEC },
        { id: 'e2', c: 'c1', from_ms: X, until_ms: X + 100 * SEC },
      ],
    });
    expect(at(m, X + 5 * SEC).creative_key).toBe('c1');
    // e1 (primeira da lista) vale de X+10 a X+20; a repetição de 30 s não cabe ⇒ FILLER com o próprio criativo
    expect(at(m, X + 15 * SEC)).toEqual(seg('EMERGENCY', 'FILLER', 'c2', X + 10 * SEC, X + 20 * SEC));
    expect(at(m, X + 25 * SEC).creative_key).toBe('c1');
  });

  it('criativo de emergência não é checado (revogado/mídia ausente) em resolve()', () => {
    const m = mk({ revoked: ['cr-c1aaaa'], emergency: [{ id: 'e1', c: 'c1', from_ms: X, until_ms: X + 30 * SEC }] });
    expect(at(m, X + 1, { revoked: [], media_ready: [] }).creative_key).toBe('c1');
  });
});

describe('função pura', () => {
  it('não muta o manifesto', () => {
    const m = mk();
    const snapshot = JSON.stringify(m);
    at(m, manaus(8, 49, 10));
    at(m, manaus(8, 50, 10), { revoked: ['cr-c1aaaa'], media_ready: [] });
    expect(JSON.stringify(m)).toBe(snapshot);
  });

  it('mídia sem vídeo/sem duração não afeta a resolução', () => {
    const m = mk({ media: { ...mk().media, m1: media('1', 'a', { duration_ms: null }) } });
    expect(at(m, A + 10 * C + I).creative_key).toBe('c1');
  });
});
