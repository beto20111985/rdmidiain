import type { MediaLite } from '@/pages/ops/api/types'

/** Durações aceitas pelo backend para criativos (segundos). */
export const ALLOWED_DURATIONS = [30, 60, 120] as const

/** Duração permitida mais próxima da duração real do vídeo (o backend confere a tolerância). */
export function suggestDuration(media: MediaLite | undefined): number | null {
  if (!media || media.kind !== 'VIDEO') return null
  const ms = media.output?.duration_ms ?? media.source.duration_ms
  if (!ms) return null
  return ALLOWED_DURATIONS.reduce<number>((best, d) => (Math.abs(d * 1000 - ms) < Math.abs(best * 1000 - ms) ? d : best), 30)
}
