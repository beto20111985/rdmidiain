/**
 * Anel de disponibilidade (DS §11): trilha azul escura, valor central. Sem dado → "—".
 * Exposto como role="img" com rótulo textual completo.
 */
import { useChartTheme } from './chart-theme'
import { EMPTY_VALUE, formatPercent } from '@/lib/format'
import { cn } from '@/lib/utils'

export interface AvailabilityRingProps {
  /** 0–100 (percentual). null = sem dado. */
  value: number | null | undefined
  label?: string
  size?: number
  className?: string
}

export function AvailabilityRing({ value, label = 'Disponibilidade', size = 140, className }: AvailabilityRingProps) {
  const theme = useChartTheme()
  const stroke = Math.max(8, Math.round(size / 12))
  const r = (size - stroke) / 2
  const c = 2 * Math.PI * r
  const has = value !== null && value !== undefined && Number.isFinite(value)
  const pct = has ? Math.min(100, Math.max(0, value as number)) : 0
  const text = has ? formatPercent(pct) : EMPTY_VALUE
  return (
    <div role="img" aria-label={`${label}: ${has ? text : 'sem dados'}`} className={cn('relative inline-flex', className)} style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={theme.track} strokeWidth={stroke} />
        {has ? (
          <circle
            cx={size / 2}
            cy={size / 2}
            r={r}
            fill="none"
            stroke={theme.series[0]}
            strokeWidth={stroke}
            strokeLinecap="round"
            strokeDasharray={`${(pct / 100) * c} ${c}`}
            transform={`rotate(-90 ${size / 2} ${size / 2})`}
          />
        ) : null}
      </svg>
      <span className="absolute inset-0 flex flex-col items-center justify-center" aria-hidden="true">
        <span className="text-kpi-secondary tabular-nums text-text">{text}</span>
        <span className="text-xs text-text-muted">{label}</span>
      </span>
    </div>
  )
}
