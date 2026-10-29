/**
 * Linha/área ao longo do tempo (DS §11): traço azul 2 px, área com gradiente 22% → 0%,
 * grade sutil, rótulos ≥ 12 px, tooltip com data + valor + unidade, no máximo 4 séries,
 * séries distintas também por traço (SERIES_DASH). Eixo Y parte de zero; recorte
 * explícito exige `yDomain` + aviso no resumo.
 * Use dentro de <ChartFrame> (que fornece resumo e tabela alternativa).
 */
import * as React from 'react'
import {
  Area,
  AreaChart as RAreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipProps,
} from 'recharts'
import { SERIES_DASH, useChartTheme } from './chart-theme'

export interface SeriesDef {
  key: string
  label: string
}

export interface TimeSeriesChartProps {
  data: Array<Record<string, string | number | null>>
  xKey: string
  series: SeriesDef[]
  /** Formata o rótulo do eixo X / título do tooltip. */
  formatX?: (v: string | number) => string
  /** Formata valores (inclua a unidade, ex.: v => `${v} exibições`). */
  formatValue?: (v: number) => string
  /** Formata os ticks do eixo Y (compacto). */
  formatTick?: (v: number) => string
  variant?: 'area' | 'line'
  height?: number
  yDomain?: [number | 'auto', number | 'auto']
}

function ChartTooltip({
  active,
  payload,
  label,
  formatX,
  formatValue,
  dashes,
}: TooltipProps<number, string> & {
  formatX?: (v: string | number) => string
  formatValue?: (v: number) => string
  dashes: Record<string, string | undefined>
}) {
  if (!active || !payload?.length) return null
  return (
    <div className="min-w-[160px] rounded-md border border-border bg-surface-raised px-3 py-2 text-xs shadow-overlay">
      <p className="mb-1 font-semibold text-text">{formatX ? formatX(label as string) : String(label)}</p>
      <ul className="m-0 flex list-none flex-col gap-1 p-0">
        {payload.map((p) => (
          <li key={String(p.dataKey)} className="flex items-center gap-2">
            <svg width="14" height="6" aria-hidden="true">
              <line x1="0" y1="3" x2="14" y2="3" stroke={p.color} strokeWidth="2" strokeDasharray={dashes[String(p.dataKey)]} />
            </svg>
            <span className="font-semibold tabular-nums text-text">
              {p.value === null || p.value === undefined ? '—' : formatValue ? formatValue(Number(p.value)) : String(p.value)}
            </span>
            <span className="text-text-muted">{p.name}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

export function TimeSeriesChart({
  data,
  xKey,
  series,
  formatX,
  formatValue,
  formatTick,
  variant = 'area',
  height = 260,
  yDomain = [0, 'auto'],
}: TimeSeriesChartProps) {
  const theme = useChartTheme()
  const gid = React.useId().replace(/:/g, '')
  const visible = series.slice(0, 4)
  if (import.meta.env.DEV && series.length > 4) console.warn('[TimeSeriesChart] Máximo de 4 séries (DS §11).')
  const dashes = Object.fromEntries(visible.map((s, i) => [s.key, SERIES_DASH[i]]))

  return (
    <div style={{ height }} className="w-full">
      <ResponsiveContainer width="100%" height="100%">
        <RAreaChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
          <defs>
            {visible.map((s, i) => (
              <linearGradient key={s.key} id={`${gid}-g${i}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={theme.series[i]} stopOpacity={0.22} />
                <stop offset="100%" stopColor={theme.series[i]} stopOpacity={0} />
              </linearGradient>
            ))}
          </defs>
          <CartesianGrid vertical={false} stroke={theme.grid} strokeWidth={1} />
          <XAxis
            dataKey={xKey}
            tickFormatter={formatX}
            tick={{ fill: theme.muted, fontSize: 12 }}
            axisLine={{ stroke: theme.grid }}
            tickLine={false}
            minTickGap={24}
          />
          <YAxis
            domain={yDomain}
            tickFormatter={formatTick}
            tick={{ fill: theme.muted, fontSize: 12 }}
            axisLine={false}
            tickLine={false}
            width={52}
            allowDecimals={false}
          />
          <Tooltip
            cursor={{ stroke: theme.muted, strokeWidth: 1 }}
            content={<ChartTooltip formatX={formatX} formatValue={formatValue} dashes={dashes} />}
          />
          {visible.map((s, i) => (
            <Area
              key={s.key}
              type="monotone"
              dataKey={s.key}
              name={s.label}
              stroke={theme.series[i]}
              strokeWidth={2}
              strokeDasharray={SERIES_DASH[i]}
              strokeLinecap="round"
              strokeLinejoin="round"
              fill={variant === 'area' ? `url(#${gid}-g${i})` : 'transparent'}
              connectNulls={false}
              dot={false}
              activeDot={{ r: 4, fill: theme.series[i], stroke: theme.surface, strokeWidth: 2 }}
              isAnimationActive={false}
            />
          ))}
        </RAreaChart>
      </ResponsiveContainer>
    </div>
  )
}
