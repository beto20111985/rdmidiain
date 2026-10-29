import type { ScreenProgramming } from '@/pages/ops/api/types'

/**
 * Mantém só os campos do contrato seguro de `GET /scheduler/screens/{id}/programming` (código, tipo,
 * nome das peças internas do próprio estabelecimento). Defesa em profundidade: qualquer campo extra
 * que o servidor venha a incluir (campanha, anunciante, preço) é descartado antes de renderizar.
 */
export function sanitizeProgramming(p: ScreenProgramming): ScreenProgramming {
  return {
    screen_id: p.screen_id,
    screen_code: p.screen_code,
    timezone: p.timezone,
    rotation: {
      internal_ms: p.rotation?.internal_ms,
      external_ms: p.rotation?.external_ms,
      cycle_ms: p.rotation?.cycle_ms,
      unit_ms: p.rotation?.unit_ms,
    },
    anchor_ms: p.anchor_ms ?? null,
    from_ms: p.from_ms,
    to_ms: p.to_ms,
    blocks: (p.blocks ?? []).map((b) => ({
      bucket_ms: b.bucket_ms,
      entries: (b.entries ?? []).map((e) => ({ code: e.code, kind: e.kind })),
    })),
    internal_pattern: (p.internal_pattern ?? []).map((blk) =>
      (blk ?? []).map((x) => ({ code: x.code, name: x.name ?? null, duration_ms: x.duration_ms })),
    ),
  }
}
