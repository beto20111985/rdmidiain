/**
 * Carregador dos vetores de conformidade (RDPP §9) — `protocol/vectors/*.json`, gerados pelo backend.
 * Floats na entrada vêm como {"$float":"1.5"} e são convertidos para float REAL (número não inteiro) ou, quando
 * o literal é integral (ex.: "1.0", que o JavaScript não distingue de 1), para `JsonFloat` — ambos rejeitados
 * pela canonicalização.
 */

import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { JsonFloat } from '../../src/core/canonical';

const here = dirname(fileURLToPath(import.meta.url));

export const VECTORS_DIR = process.env.RD_VECTORS_DIR ?? resolve(here, '../../../protocol/vectors');

export interface VectorFile<C> {
  version: number;
  generated_by: string;
  cases: C[];
  [k: string]: unknown;
}

export function vectorPath(name: string): string {
  return join(VECTORS_DIR, name);
}

export function hasVector(name: string): boolean {
  return existsSync(vectorPath(name));
}

/** Converte marcadores {"$float": "..."} (recursivo). */
export function reviveFloats(v: unknown): unknown {
  if (Array.isArray(v)) return v.map(reviveFloats);
  if (v && typeof v === 'object') {
    const o = v as Record<string, unknown>;
    const keys = Object.keys(o);
    if (keys.length === 1 && keys[0] === '$float' && typeof o.$float === 'string') {
      const n = Number(o.$float);
      return Number.isFinite(n) && !Number.isInteger(n) ? n : new JsonFloat(o.$float);
    }
    const out: Record<string, unknown> = {};
    for (const k of keys) out[k] = reviveFloats(o[k]);
    return out;
  }
  return v;
}

export function loadVector<C>(name: string, opts: { revive?: boolean } = {}): VectorFile<C> {
  const raw = JSON.parse(readFileSync(vectorPath(name), 'utf8')) as VectorFile<C>;
  if (raw.version !== 1 || !Array.isArray(raw.cases)) throw new Error(`${name}: formato inesperado (version/cases)`);
  return opts.revive ? (reviveFloats(raw) as VectorFile<C>) : raw;
}
