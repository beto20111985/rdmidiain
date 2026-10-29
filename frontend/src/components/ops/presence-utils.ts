import type { PresenceSummary } from '@/pages/ops/api/types'

/** Último contato em ms epoch (preferido) ou ISO. */
export function lastContactOf(p: Pick<PresenceSummary, 'last_seen_ms' | 'last_seen_at'> | null | undefined): Date | null {
  if (!p) return null
  if (typeof p.last_seen_ms === 'number' && p.last_seen_ms > 0) return new Date(p.last_seen_ms)
  if (p.last_seen_at) {
    const d = new Date(p.last_seen_at)
    return Number.isNaN(d.getTime()) ? null : d
  }
  return null
}

/** % do download da programação pendente (`download` do W-PLAY); sem dado → null ("—"). */
export function downloadPercent(p: PresenceSummary | null | undefined): number | null {
  const d = p?.download
  if (!d || !d.total_bytes || d.total_bytes <= 0 || d.done_bytes === null || d.done_bytes === undefined) return null
  return Math.max(0, Math.min(100, Math.round((d.done_bytes / d.total_bytes) * 100)))
}
