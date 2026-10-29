import { useMemo } from 'react'
import { useThemeStore } from '@/stores/themeStore'

/**
 * Cores dos gráficos lidas dos tokens CSS (atributos SVG não resolvem var()).
 * Recalcula quando o tema muda. Ordem fixa das séries (nunca reciclar cores):
 * chart-1 → chart-2 → chart-3 → chart-4; acima de 4 séries, agrupar em "Outros" (DS §11).
 */
export interface ChartTheme {
  series: [string, string, string, string]
  grid: string
  track: string
  text: string
  muted: string
  surface: string
  raised: string
  border: string
}

const FALLBACK: ChartTheme = {
  series: ['#0b84ff', '#00b9f2', '#5bceff', '#9eafd1'],
  grid: '#203859',
  track: '#082a4a',
  text: '#f5f7fa',
  muted: '#a6afbb',
  surface: '#081426',
  raised: '#0d1c32',
  border: '#203859',
}

export function readChartTheme(): ChartTheme {
  if (typeof window === 'undefined') return FALLBACK
  const cs = window.getComputedStyle(document.documentElement)
  const rgb = (name: string, fb: string) => {
    const raw = cs.getPropertyValue(`--${name}`).trim()
    if (!raw) return fb
    const parts = raw.split(/\s+/)
    return parts.length === 3 ? `rgb(${parts.join(',')})` : fb
  }
  return {
    series: [
      rgb('chart-1', FALLBACK.series[0]),
      rgb('chart-2', FALLBACK.series[1]),
      rgb('chart-3', FALLBACK.series[2]),
      rgb('chart-4', FALLBACK.series[3]),
    ],
    grid: rgb('chart-grid', FALLBACK.grid),
    track: rgb('chart-track', FALLBACK.track),
    text: rgb('text', FALLBACK.text),
    muted: rgb('text-muted', FALLBACK.muted),
    surface: rgb('surface', FALLBACK.surface),
    raised: rgb('surface-raised', FALLBACK.raised),
    border: rgb('border', FALLBACK.border),
  }
}

export function useChartTheme(): ChartTheme {
  const resolved = useThemeStore((s) => s.resolved)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  return useMemo(() => readChartTheme(), [resolved])
}

/**
 * Codificação secundária obrigatória (DS §11: não distinguir séries só por tons próximos).
 * Série 1 contínua, 2 tracejada, 3 pontilhada, 4 traço-ponto.
 */
export const SERIES_DASH: Array<string | undefined> = [undefined, '6 4', '2 3', '8 3 2 3']
