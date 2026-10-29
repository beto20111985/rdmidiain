/**
 * Barras (DS §11): mesma escala, rótulos claros, SEMPRE a partir de zero. Barras com até
 * 24 px de espessura, ponta arredondada de 4 px e base reta; valor na ponta.
 * Use dentro de <ChartFrame>.
 */
import {
  Bar,
  BarChart as RBarChart,
  CartesianGrid,
  LabelList,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipProps,
} from 'recharts'
import { useChartTheme } from './chart-theme'
import { formatNumber } from '@/lib/format'

export interface BarDatum {
  label: string
  value: number
}

export interface BarChartProps {
  data: BarDatum[]
  formatValue?: (v: number) => string
  /** horizontal = categorias no eixo Y (melhor para nomes longos). */
  orientation?: 'horizontal' | 'vertical'
  height?: number
}

function BarTooltip({ active, payload, formatValue }: TooltipProps<number, string> & { formatValue: (v: number) => string }) {
  if (!active || !payload?.length) return null
  const p = payload[0]
  return (
    <div className="rounded-md border border-border bg-surface-raised px-3 py-2 text-xs shadow-overlay">
      <span className="font-semibold tabular-nums text-text">{formatValue(Number(p.value))}</span>{' '}
      <span className="text-text-muted">{(p.payload as BarDatum).label}</span>
    </div>
  )
}

export function BarChart({ data, formatValue = (v) => formatNumber(v), orientation = 'horizontal', height }: BarChartProps) {
  const theme = useChartTheme()
  const horizontal = orientation === 'horizontal'
  const h = height ?? (horizontal ? Math.max(120, data.length * 40 + 24) : 260)
  return (
    <div style={{ height: h }} className="w-full">
      <ResponsiveContainer width="100%" height="100%">
        <RBarChart
          data={data}
          layout={horizontal ? 'vertical' : 'horizontal'}
          margin={{ top: 8, right: horizontal ? 56 : 12, bottom: 0, left: 0 }}
          barCategoryGap={8}
        >
          <CartesianGrid horizontal={!horizontal} vertical={horizontal} stroke={theme.grid} strokeWidth={1} />
          {horizontal ? (
            <>
              <XAxis type="number" domain={[0, 'auto']} hide />
              <YAxis
                type="category"
                dataKey="label"
                width={140}
                tick={{ fill: theme.text, fontSize: 12 }}
                axisLine={false}
                tickLine={false}
              />
            </>
          ) : (
            <>
              <XAxis dataKey="label" tick={{ fill: theme.muted, fontSize: 12 }} axisLine={{ stroke: theme.grid }} tickLine={false} />
              <YAxis domain={[0, 'auto']} tick={{ fill: theme.muted, fontSize: 12 }} axisLine={false} tickLine={false} width={48} />
            </>
          )}
          <Tooltip cursor={{ fill: theme.grid, fillOpacity: 0.35 }} content={<BarTooltip formatValue={formatValue} />} />
          <Bar
            dataKey="value"
            fill={theme.series[0]}
            maxBarSize={24}
            radius={horizontal ? [0, 4, 4, 0] : [4, 4, 0, 0]}
            isAnimationActive={false}
          >
            <LabelList
              dataKey="value"
              position={horizontal ? 'right' : 'top'}
              formatter={(v: number) => formatValue(v)}
              style={{ fill: theme.text, fontSize: 12, fontWeight: 600 }}
            />
          </Bar>
        </RBarChart>
      </ResponsiveContainer>
    </div>
  )
}
