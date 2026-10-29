export interface DonutDatum {
  label: string
  value: number
}

/** Rosca com no máximo `max` fatias: as menores viram "Outros" (DS §11). */
export function groupSlices(data: DonutDatum[], max = 4, otherLabel = 'Outros'): DonutDatum[] {
  const sorted = [...data].filter((d) => d.value > 0).sort((a, b) => b.value - a.value)
  if (sorted.length <= max) return sorted
  const head = sorted.slice(0, max - 1)
  const rest = sorted.slice(max - 1).reduce((acc, d) => acc + d.value, 0)
  return [...head, { label: otherLabel, value: rest }]
}
