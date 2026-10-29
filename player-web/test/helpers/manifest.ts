import type { CreativeEntry, ExternalEntry, Manifest, MediaEntry, PatternItem } from '../../src/core/protocol';

/** Âncora do exemplo do A6 §10.6: 08:07:00 em America/Manaus (UTC−4) = 12:07:00Z. */
export const ANCHOR_0807 = Date.UTC(2026, 9, 7, 12, 7, 0);
export const MIN = 60_000;
export const SEC = 1_000;
export const DAY = 86_400_000;
export const WEEK = 7 * DAY;

/** Instante local de Manaus (UTC−4) no dia da âncora. */
export function manaus(h: number, m: number, s = 0, ms = 0): number {
  return Date.UTC(2026, 9, 7, h + 4, m, s, ms);
}

function sha(ch: string): string {
  return ch.repeat(64);
}

export function media(key: string, ch: string, opts: Partial<MediaEntry> = {}): MediaEntry {
  return {
    media_id: `00000000-0000-4000-8000-0000000000${key.padStart(2, '0').slice(-2)}`,
    sha256: sha(ch),
    size: 1000,
    mime: 'image/jpeg',
    width: 1920,
    height: 1080,
    duration_ms: null,
    ...opts,
  };
}

export function creative(id: string, kind: CreativeEntry['kind'], mediaKey: string, opts: Partial<CreativeEntry> = {}): CreativeEntry {
  const labels: Record<string, string> = {
    EXTERNAL: 'Publicidade',
    INSTITUTIONAL: 'Publicidade',
    INTERNAL: 'Conteúdo do estabelecimento',
    FALLBACK: 'RD Mídia Indoor',
  };
  return {
    creative_id: `cr-${id}`,
    code: `RD-${id.toUpperCase().padEnd(6, 'X').slice(0, 6)}`,
    kind,
    campaign_id: kind === 'EXTERNAL' || kind === 'INSTITUTIONAL' ? `camp-${id}` : null,
    advertiser_id: kind === 'EXTERNAL' ? `adv-${id}` : null,
    media: mediaKey,
    duration_ms: 30_000,
    valid_from_ms: 0,
    valid_until_ms: 4_102_444_800_000, // 2100-01-01
    label: labels[kind]!,
    ...opts,
  };
}

export interface MkOptions {
  anchor?: number | null;
  internalMs?: number;
  externalMs?: number;
  /** Entradas por bucket (gerado para todo o horizonte). */
  entries?: (bucket: number) => ExternalEntry[];
  internalPattern?: PatternItem[][];
  fallback?: PatternItem[];
  horizonFrom?: number;
  horizonDays?: number;
  creatives?: Record<string, CreativeEntry>;
  media?: Record<string, MediaEntry>;
  emergency?: Manifest['emergency'];
  revoked?: string[];
  version?: number;
}

export function mk(o: MkOptions = {}): Manifest {
  const I = o.internalMs ?? 120_000;
  const E = o.externalMs ?? 120_000;
  const C = I + E;
  const from = o.horizonFrom ?? Math.floor(ANCHOR_0807 / C) * C;
  const to = from + (o.horizonDays ?? 7) * DAY;
  const plan: Record<string, ExternalEntry[]> = {};
  const entries =
    o.entries ??
    ((b: number) => [
      { i: `ins-${b}-0`, c: 'c1', u: 0, d: 30_000 },
      { i: `ins-${b}-1`, c: 'c2', u: 1, d: 30_000 },
    ]);
  for (let b = from; b < to; b += C) {
    const e = entries(b);
    if (e.length) plan[String(b)] = e;
  }
  return {
    schema: 'rd.manifest/1',
    manifest_id: '11111111-1111-4111-8111-111111111111',
    version: o.version ?? 42,
    screen_id: 'scr-1',
    screen_code: 'TEL-AM-7Q3M',
    generated_at_ms: from,
    timezone: 'America/Manaus',
    rotation: { internal_ms: I, external_ms: E, cycle_ms: C, unit_ms: 30_000, policy_id: 'pol-1' },
    anchor_ms: o.anchor === undefined ? ANCHOR_0807 : o.anchor,
    horizon: { from_ms: from, to_ms: to, repeat_period_ms: WEEK },
    settings: {
      image_default_ms: 30_000,
      fade_ms: 400,
      orientation: 'LANDSCAPE',
      footer: { enabled: true, height_permille: 60 },
      offline_alert_days: [7, 28],
      heartbeat_interval_ms: 30_000,
      telemetry_interval_ms: 300_000,
      panel_url: 'https://painel.exemplo.invalid',
    },
    media: o.media ?? {
      m1: media('1', 'a'),
      m2: media('2', 'b'),
      m7: media('7', 'c', { mime: 'video/mp4', duration_ms: 60_000 }),
      m8: media('8', 'd'),
      mf: media('9', 'e'),
      mg: media('10', 'f', { mime: 'video/mp4', duration_ms: 60_000 }),
    },
    creatives: o.creatives ?? {
      c1: creative('c1aaaa', 'EXTERNAL', 'm1'),
      c2: creative('c2bbbb', 'EXTERNAL', 'm2'),
      c7: creative('c7cccc', 'INTERNAL', 'm7', { duration_ms: 60_000 }),
      c8: creative('c8dddd', 'INTERNAL', 'm8', { duration_ms: 60_000 }),
      f1: creative('f1eeee', 'FALLBACK', 'mf'),
      g1: creative('g1ffff', 'EXTERNAL', 'mg', { duration_ms: 60_000 }),
    },
    internal_pattern: o.internalPattern ?? [
      [
        { c: 'c7', d: 60_000 },
        { c: 'c8', d: 60_000 },
      ],
    ],
    fallback: o.fallback ?? [{ c: 'f1', d: 30_000 }],
    external_plan: plan,
    emergency: o.emergency ?? [],
    revoked: o.revoked ?? [],
  };
}

export const ALL_MEDIA = ['m1', 'm2', 'm7', 'm8', 'mf', 'mg'];
