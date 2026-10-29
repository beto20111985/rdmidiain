/**
 * Sparkline (DS §11): traço de 2 px, sem eixos (o contexto vem do cartão), decorativo
 * para leitores de tela — o valor e a variação estão em texto no KPI.
 */
import * as React from 'react'
import { useChartTheme } from './chart-theme'
import { cn } from '@/lib/utils'

export function Sparkline({ data, className }: { data: Array<number | null>; className?: string }) {
  const theme = useChartTheme()
  const id = React.useId().replace(/:/g, '')
  const values = data.map((v) => (v === null || !Number.isFinite(v) ? null : v))
  const nums = values.filter((v): v is number => v !== null)
  if (nums.length < 2) return null
  const min = Math.min(...nums)
  const max = Math.max(...nums)
  const span = max - min || 1
  const W = 96
  const H = 40
  const stepX = W / (values.length - 1)
  const pts = values.map((v, i) => (v === null ? null : [i * stepX, H - 3 - ((v - min) / span) * (H - 6)] as const))
  const segs: string[] = []
  let cur = ''
  pts.forEach((p) => {
    if (!p) {
      if (cur) segs.push(cur)
      cur = ''
      return
    }
    cur += `${cur ? 'L' : 'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}`
  })
  if (cur) segs.push(cur)
  const last = [...pts].reverse().find(Boolean)
  return (
    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true" className={cn('overflow-visible', className)}>
      <defs>
        <linearGradient id={`${id}-sg`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={theme.series[0]} stopOpacity={0.22} />
          <stop offset="100%" stopColor={theme.series[0]} stopOpacity={0} />
        </linearGradient>
      </defs>
      {segs.map((d, i) => (
        <path key={i} d={d} fill="none" stroke={theme.series[0]} strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
      ))}
      {last ? <circle cx={last[0]} cy={last[1]} r={3} fill={theme.series[0]} stroke={theme.surface} strokeWidth={2} vectorEffect="non-scaling-stroke" /> : null}
    </svg>
  )
}
