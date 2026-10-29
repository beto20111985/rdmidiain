/** Cálculos de grade (RDPP/1 §5) usados nas prévias de programação. */

/** Início do bloco externo dentro do bucket (RDPP/1 §5: s = A + k·C + I). */
export function externalBlockStart(bucketMs: number, anchorMs: number | null, cycleMs: number, internalMs: number | undefined): number | null {
  if (anchorMs === null || !cycleMs || internalMs === undefined) return null
  const k = Math.ceil((bucketMs - anchorMs - internalMs) / cycleMs)
  return anchorMs + internalMs + k * cycleMs
}
