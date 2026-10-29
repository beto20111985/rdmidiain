/**
 * Rosca (DS §11): azul, ciano e azul claro; total no centro; no máximo 4 fatias —
 * categorias excedentes são agrupadas em "Outros" (ou prefira barras). Separação de
 * 2 px na cor da superfície entre fatias; a legenda traz valor e percentual (identidade
 * nunca só por cor). Use dentro de <ChartFrame>.
 */
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip, type TooltipProps } from 'recharts'
import { useChartTheme } from './chart-theme'
import { ChartLegend } from './chart-frame'
import { formatNumber, formatPercent } from '@/lib/format'

import { groupSlices, type DonutDatum } from './utils'

export type { DonutDatum }

export interface DonutChartProps {
  data: DonutDatum[]
  totalLabel?: string
  formatValue?: (v: number) => string
  size?: number
}

function DonutTooltip({ active, payload, formatValue }: TooltipProps<number, string> & { formatValue: (v: number) => string }) {
  if (!active || !payload?.length) return null
  const p = payload[0]
  return (
    <div className="rounded-md border border-border bg-surface-raised px-3 py-2 text-xs shadow-overlay">
      <span className="font-semibold tabular-nums text-text">{formatValue(Number(p.value))}</span>{' '}
      <span className="text-text-muted">{p.name}</span>
    </div>
  )
}

export function DonutChart({ data, totalLabel = 'Total', formatValue = (v) => formatNumber(v), size = 200 }: DonutChartProps) {
  const theme = useChartTheme()
  const slices = groupSlices(data)
  const total = slices.reduce((a, d) => a + d.value, 0)
  return (
    <div className="flex flex-col items-center gap-4 sm:flex-row sm:items-center sm:gap-6">
      <div className="relative shrink-0" style={{ width: size, height: size }}>
        <ResponsiveContainer width="100%" height="100%">
          <PieChart>
            <Pie
              data={slices}
              dataKey="value"
              nameKey="label"
              innerRadius="68%"
              outerRadius="100%"
              startAngle={90}
              endAngle={-270}
              stroke={theme.surface}
              strokeWidth={2}
              isAnimationActive={false}
            >
              {slices.map((s, i) => (
                <Cell key={s.label} fill={theme.series[i % 4]} />
              ))}
            </Pie>
            <Tooltip content={<DonutTooltip formatValue={formatValue} />} />
          </PieChart>
        </ResponsiveContainer>
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center text-center">
          <span className="text-xs text-text-muted">{totalLabel}</span>
          <span className="text-kpi-secondary tabular-nums text-text">{formatValue(total)}</span>
        </div>
      </div>
      <ChartLegend
        items={slices.map((s, i) => ({
          label: s.label,
          color: theme.series[i % 4],
          kind: 'swatch' as const,
          value: `${formatValue(s.value)} · ${total ? formatPercent((s.value / total) * 100) : '—'}`,
        }))}
      />
    </div>
  )
}
