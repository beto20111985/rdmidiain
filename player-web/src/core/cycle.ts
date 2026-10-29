/**
 * Resolução normativa do ciclo ancorado — RDPP §5.1 e §5.2 (+ esclarecimentos §8.2).
 *
 * `resolve(manifest, anchor_ms, local_state, t)` é FUNÇÃO PURA (é ela que os vetores de conformidade testam;
 * o Android implementa as mesmas regras). Ela devolve o segmento PROGRAMADO que contém `t` — a regra de
 * "entrada no meio do item" (FILLER + SKIPPED) é do runtime (`playback.ts`), não daqui.
 *
 * Regras (resumo):
 *  - d = t − A; k = floor(d/C); fase = d − k·C. fase < I ⇒ bloco INTERNO, senão EXTERNO.
 *  - Interno: internal_pattern[k mod len] a partir de A + k·C; item que ultrapassa o fim do bloco não toca e o
 *    resto do bloco vai para fallback; padrão vazio ⇒ bloco inteiro em fallback.
 *  - Externo: s = A + k·C + I; b = floor(s/C)·C; fora do horizonte ⇒ b' = b − n·P (menor |n|); entradas de
 *    external_plan[b'] tocam em [s + u·U, s + u·U + d). Lacunas máximas, entradas inválidas, que ultrapassam o
 *    bloco ou sobrepostas ⇒ fallback (cada uma é um intervalo próprio; sem fusão).
 *  - Validade no INÍCIO do item: valid_from ≤ início < valid_until, mídia em media_ready, criativo fora de
 *    revoked (manifesto + local). Fallback e emergência NÃO são checados (§8.2.1).
 *  - Fallback de [s,e): ordem cíclica a partir de floor((s−A)/U) mod len; item só entra inteiro; o resto menor
 *    que o próximo item vira FILLER com fallback[índice] (sem PoP); fallback vazio ⇒ FILLER null.
 *  - Emergência: primeira entrada com from ≤ t < until; repete creatives[c].duration_ms a partir de from; a
 *    última repetição que não cabe até until vira FILLER com o próprio criativo (block EMERGENCY).
 */

import type { CreativeEntry, ExternalEntry, Manifest } from './protocol';
import { mod } from './util';

export type Block = 'INTERNAL' | 'EXTERNAL' | 'EMERGENCY';
export type SegmentKind = 'ITEM' | 'FILLER';

/** Saída normativa (formato dos vetores `cycle_resolution.json`). */
export interface Segment {
  block: Block;
  kind: SegmentKind;
  creative_key: string | null;
  start_ms: number;
  end_ms: number;
}

export interface LocalState {
  /** creative_id revogados localmente (kills), somados ao `manifest.revoked`. */
  revoked: readonly string[];
  /** chaves de `manifest.media` presentes e com sha256 verificado. */
  media_ready: readonly string[];
}

export type SegmentSource = 'PATTERN' | 'PLAN' | 'FALLBACK' | 'EMERGENCY';

/** Segmento com metadados de runtime (inserção, origem, ciclo). */
export interface DetailedSegment extends Segment {
  source: SegmentSource;
  insertion_id: string | null;
  emergency_id: string | null;
  /** índice k do ciclo em que o segmento foi gerado (emergência: k do instante consultado). */
  cycle: number;
}

/** Contexto pré-processado (evita recriar Sets a cada consulta). */
export interface ResolveContext {
  manifest: Manifest;
  anchorMs: number;
  revoked: ReadonlySet<string>;
  mediaReady: ReadonlySet<string>;
}

export function createResolveContext(manifest: Manifest, anchorMs: number, local: LocalState): ResolveContext {
  return {
    manifest,
    anchorMs,
    revoked: new Set<string>([...manifest.revoked, ...local.revoked]),
    mediaReady: new Set<string>(local.media_ready),
  };
}

/** API normativa (§5.2). */
export function resolve(manifest: Manifest, anchor_ms: number, local_state: LocalState, t: number): Segment {
  const s = resolveDetailed(createResolveContext(manifest, anchor_ms, local_state), t);
  return { block: s.block, kind: s.kind, creative_key: s.creative_key, start_ms: s.start_ms, end_ms: s.end_ms };
}

/** Segmento programado em `t`, considerando emergência. */
export function resolveDetailed(ctx: ResolveContext, t: number): DetailedSegment {
  const em = emergencySegment(ctx, t);
  return em ?? resolveRegular(ctx, t);
}

/** Primeira emergência ativa em `t` (ordem da lista), ou null. */
export function activeEmergency(manifest: Manifest, t: number): Manifest['emergency'][number] | null {
  for (const e of manifest.emergency) {
    if (e.from_ms <= t && t < e.until_ms) return e;
  }
  return null;
}

function emergencySegment(ctx: ResolveContext, t: number): DetailedSegment | null {
  const e = activeEmergency(ctx.manifest, t);
  if (!e) return null;
  const k = cycleIndex(ctx, t);
  const step = ctx.manifest.creatives[e.c]?.duration_ms ?? 0;
  if (!(step > 0)) {
    // Criativo de emergência inexistente/sem duração: janela inteira como FILLER (tela embutida se faltar mídia).
    return seg('EMERGENCY', 'FILLER', e.c, e.from_ms, e.until_ms, 'EMERGENCY', null, e.id, k);
  }
  const j = Math.floor((t - e.from_ms) / step);
  const start = e.from_ms + j * step;
  const end = start + step;
  if (end > e.until_ms) return seg('EMERGENCY', 'FILLER', e.c, start, e.until_ms, 'EMERGENCY', null, e.id, k);
  return seg('EMERGENCY', 'ITEM', e.c, start, end, 'EMERGENCY', null, e.id, k);
}

export function cycleIndex(ctx: ResolveContext, t: number): number {
  return Math.floor((t - ctx.anchorMs) / ctx.manifest.rotation.cycle_ms);
}

/** Início do ciclo k. */
export function cycleStart(ctx: ResolveContext, k: number): number {
  return ctx.anchorMs + k * ctx.manifest.rotation.cycle_ms;
}

/** true se `t` é fronteira de ciclo (fase == 0). */
export function isCycleBoundary(manifest: Manifest, anchorMs: number, t: number): boolean {
  return mod(t - anchorMs, manifest.rotation.cycle_ms) === 0;
}

/** Segmento regular (sem emergência) que contém `t`. */
export function resolveRegular(ctx: ResolveContext, t: number): DetailedSegment {
  const segs = blockSegmentsAt(ctx, t);
  for (const s of segs) {
    if (s.start_ms <= t && t < s.end_ms) return s;
  }
  // Inalcançável: os segmentos cobrem o bloco inteiro de forma contígua.
  throw new Error(`resolução sem cobertura em t=${t}`);
}

/** Todos os segmentos (contíguos) do bloco que contém `t`. */
export function blockSegmentsAt(ctx: ResolveContext, t: number): DetailedSegment[] {
  const { cycle_ms: C, internal_ms: I } = ctx.manifest.rotation;
  const A = ctx.anchorMs;
  const k = Math.floor((t - A) / C);
  const fase = t - A - k * C;
  if (fase < I) return internalBlock(ctx, k);
  return externalBlock(ctx, k);
}

function internalBlock(ctx: ResolveContext, k: number): DetailedSegment[] {
  const m = ctx.manifest;
  const start = ctx.anchorMs + k * m.rotation.cycle_ms;
  const end = start + m.rotation.internal_ms;
  const out: DetailedSegment[] = [];
  if (m.rotation.internal_ms <= 0) return out;
  const pattern = m.internal_pattern;
  if (pattern.length === 0) {
    fill(ctx, 'INTERNAL', start, end, k, out);
    return out;
  }
  const sequence = pattern[mod(k, pattern.length)] ?? [];
  let pos = start;
  for (const item of sequence) {
    if (!(item.d > 0)) continue; // defesa: duração inválida não ocupa tempo
    if (pos + item.d > end) break; // não cabe: o resto do bloco vai para fallback
    if (isPlayable(ctx, item.c, pos)) {
      out.push(seg('INTERNAL', 'ITEM', item.c, pos, pos + item.d, 'PATTERN', null, null, k));
    } else {
      fill(ctx, 'INTERNAL', pos, pos + item.d, k, out);
    }
    pos += item.d;
  }
  if (pos < end) fill(ctx, 'INTERNAL', pos, end, k, out);
  return out;
}

/** Mapeia o bucket para dentro do horizonte (§5.2 "Horizonte"); null se não cair. */
export function mapBucketToHorizon(manifest: Manifest, b: number): number | null {
  const { from_ms: from, to_ms: to, repeat_period_ms: P } = manifest.horizon;
  if (from <= b && b < to) return b;
  if (!(P > 0)) return null;
  if (b >= to) {
    const n = Math.ceil((b - to + 1) / P);
    const mapped = b - n * P;
    return mapped >= from && mapped < to ? mapped : null;
  }
  const n = Math.ceil((from - b) / P);
  const mapped = b + n * P;
  return mapped >= from && mapped < to ? mapped : null;
}

function externalBlock(ctx: ResolveContext, k: number): DetailedSegment[] {
  const m = ctx.manifest;
  const { cycle_ms: C, internal_ms: I, unit_ms: U } = m.rotation;
  const s = ctx.anchorMs + k * C + I;
  const blockEnd = ctx.anchorMs + (k + 1) * C;
  const out: DetailedSegment[] = [];
  if (blockEnd <= s) return out;
  const b = Math.floor(s / C) * C;
  const mapped = mapBucketToHorizon(m, b);
  const entries = mapped === null ? undefined : m.external_plan[String(mapped)];
  if (!entries || entries.length === 0) {
    fill(ctx, 'EXTERNAL', s, blockEnd, k, out);
    return out;
  }
  const sorted: ExternalEntry[] = entries
    .map((e, idx) => ({ e, idx }))
    .sort((x, y) => x.e.u - y.e.u || x.idx - y.idx)
    .map((x) => x.e);
  // Algoritmo normativo §8.2.4: cursor = fim do último intervalo ocupado.
  let cursor = s;
  for (const e of sorted) {
    const es = s + e.u * U;
    const ee = es + e.d;
    if (es >= blockEnd) continue; // início ≥ fim do bloco: ignorada
    if (es > cursor) {
      fill(ctx, 'EXTERNAL', cursor, es, k, out); // lacuna máxima entre entradas = intervalo próprio
      cursor = es;
    }
    if (es < cursor || ee > blockEnd || !(e.d > 0)) {
      // Sobreposição (a primeira do par já tocou) ou ultrapassa o bloco: não toca;
      // [max(início, cursor), min(fim, fim_do_bloco)) vira fallback.
      const fe = Math.min(ee, blockEnd);
      if (fe > cursor) {
        fill(ctx, 'EXTERNAL', cursor, fe, k, out);
        cursor = fe;
      }
      continue;
    }
    if (isPlayable(ctx, e.c, es)) {
      out.push(seg('EXTERNAL', 'ITEM', e.c, es, ee, 'PLAN', e.i, null, k));
    } else {
      fill(ctx, 'EXTERNAL', es, ee, k, out); // item inválido = intervalo próprio
    }
    cursor = ee;
  }
  if (cursor < blockEnd) fill(ctx, 'EXTERNAL', cursor, blockEnd, k, out);
  return out;
}

/** Validade de um item interno/externo no seu INÍCIO. */
export function isPlayable(ctx: ResolveContext, creativeKey: string, startMs: number): boolean {
  const c: CreativeEntry | undefined = ctx.manifest.creatives[creativeKey];
  if (!c) return false;
  if (ctx.revoked.has(c.creative_id)) return false;
  if (!(c.valid_from_ms <= startMs && startMs < c.valid_until_ms)) return false;
  return ctx.mediaReady.has(c.media);
}

/** Preenchimento por fallback de [s, e) — §5.2. */
function fill(ctx: ResolveContext, block: Block, s: number, e: number, k: number, out: DetailedSegment[]): void {
  if (e <= s) return;
  const fb = ctx.manifest.fallback;
  if (fb.length === 0) {
    out.push(seg(block, 'FILLER', null, s, e, 'FALLBACK', null, null, k));
    return;
  }
  let idx = mod(Math.floor((s - ctx.anchorMs) / ctx.manifest.rotation.unit_ms), fb.length);
  let pos = s;
  while (pos < e) {
    const item = fb[idx]!;
    if (item.d > 0 && pos + item.d <= e) {
      out.push(seg(block, 'ITEM', item.c, pos, pos + item.d, 'FALLBACK', null, null, k));
      pos += item.d;
      idx = (idx + 1) % fb.length;
    } else {
      out.push(seg(block, 'FILLER', item.c, pos, e, 'FALLBACK', null, null, k));
      pos = e;
    }
  }
}

/** Criativo de fallback para exibir num FILLER de runtime (entrada no meio do item) começando em `t`. */
export function fallbackKeyAt(ctx: ResolveContext, t: number): string | null {
  const fb = ctx.manifest.fallback;
  if (fb.length === 0) return null;
  return fb[mod(Math.floor((t - ctx.anchorMs) / ctx.manifest.rotation.unit_ms), fb.length)]!.c;
}

/**
 * Primeiro instante em (after, before) em que a resolução muda por emergência (início ou fim de alguma janela).
 * Usado pelo runtime para não iniciar um item que seria preemptado.
 */
export function nextEmergencyChange(manifest: Manifest, after: number, before: number): number | null {
  let best: number | null = null;
  for (const e of manifest.emergency) {
    if (e.until_ms <= e.from_ms) continue;
    for (const p of [e.from_ms, e.until_ms]) {
      if (p > after && p < before && (best === null || p < best)) best = p;
    }
  }
  return best;
}

function seg(
  block: Block,
  kind: SegmentKind,
  creative_key: string | null,
  start_ms: number,
  end_ms: number,
  source: SegmentSource,
  insertion_id: string | null,
  emergency_id: string | null,
  cycle: number,
): DetailedSegment {
  return { block, kind, creative_key, start_ms, end_ms, source, insertion_id, emergency_id, cycle };
}
