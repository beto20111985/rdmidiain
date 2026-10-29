/**
 * Cartão de KPI (DS §10/§12): rótulo, número, unidade, comparação temporal SEMPRE com o
 * período, gráfico opcional.
 * - Sem dado → "—" (nunca zero inventado).
 * - Base anterior zero/ausente → "Sem base de comparação".
 * - Polaridade explícita: queda nem sempre é ruim (menos falhas = melhora).
 */
import * as React from 'react'
import { ArrowDownRight, ArrowRight, ArrowUpRight } from 'lucide-react'
import { Card } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'
import { EMPTY_VALUE, formatNumber } from '@/lib/format'
import { Sparkline } from '@/components/charts/sparkline'
import { DemoBadge } from './states'

export type KpiPolarity = 'higher-is-better' | 'lower-is-better' | 'neutral'

export interface KpiComparison {
  /** Variação percentual (ex.: 4.2 = +4,2%). null/undefined = sem base. */
  deltaPct?: number | null
  /** Ex.: "vs. 7 dias anteriores". Obrigatório: variação sem período não é exibida. */
  periodLabel: string
  polarity?: KpiPolarity
}

export interface KpiCardProps {
  label: string
  value: number | null | undefined
  format?: (v: number) => string
  unit?: string
  comparison?: KpiComparison
  /** Série curta para sparkline (tendência). */
  trend?: Array<number | null>
  description?: string
  loading?: boolean
  /** Exige rótulo DEMONSTRAÇÃO quando o valor não é real. */
  demo?: boolean
  emphasis?: 'primary' | 'secondary'
  headingLevel?: 'h2' | 'h3'
  className?: string
  footer?: React.ReactNode
}

function DeltaLine({ comparison }: { comparison: KpiComparison }) {
  const { deltaPct, periodLabel, polarity = 'higher-is-better' } = comparison
  if (deltaPct === null || deltaPct === undefined || !Number.isFinite(deltaPct)) {
    return <p className="text-xs text-text-muted">Sem base de comparação</p>
  }
  const direction = deltaPct > 0 ? 'up' : deltaPct < 0 ? 'down' : 'flat'
  const good =
    polarity === 'neutral' || direction === 'flat'
      ? null
      : (direction === 'up') === (polarity === 'higher-is-better')
  const Icon = direction === 'up' ? ArrowUpRight : direction === 'down' ? ArrowDownRight : ArrowRight
  const formatted = `${deltaPct > 0 ? '+' : ''}${formatNumber(deltaPct, { maximumFractionDigits: 1, minimumFractionDigits: 1 })}%`
  const verdict = good === null ? '' : good ? ' (melhora)' : ' (piora)'
  return (
    <p className="flex flex-wrap items-center gap-x-1.5 text-xs">
      <span
        className={cn(
          'inline-flex items-center gap-0.5 font-semibold tabular-nums',
          good === null ? 'text-text' : good ? 'text-success' : 'text-danger',
        )}
      >
        <Icon aria-hidden="true" className="size-4" />
        {formatted}
        <span className="sr-only">{verdict}</span>
      </span>
      <span className="text-text-muted">{periodLabel}</span>
    </p>
  )
}

export function KpiCard({
  label,
  value,
  format = (v) => formatNumber(v),
  unit,
  comparison,
  trend,
  description,
  loading = false,
  demo = false,
  emphasis = 'primary',
  headingLevel = 'h2',
  className,
  footer,
}: KpiCardProps) {
  const Heading = headingLevel
  const hasValue = value !== null && value !== undefined && Number.isFinite(value)
  return (
    <Card className={cn('gap-3', className)} aria-busy={loading || undefined}>
      <div className="flex items-start justify-between gap-3">
        <Heading className="rd-metric-label">{label}</Heading>
        {demo ? <DemoBadge /> : null}
      </div>
      {loading ? (
        <div className="flex flex-col gap-2" role="status">
          <span className="sr-only">Carregando {label}…</span>
          <Skeleton className={emphasis === 'primary' ? 'h-12 w-32' : 'h-9 w-24'} />
          <Skeleton className="h-4 w-40" />
        </div>
      ) : (
        <>
          <div className="flex items-end justify-between gap-4">
            <p className="flex min-w-0 items-baseline gap-2">
              <span
                data-numeric
                className={cn(
                  'tabular-nums text-text',
                  emphasis === 'primary' ? 'text-kpi-sm xl:text-kpi' : 'text-kpi-secondary',
                )}
              >
                {hasValue ? format(value as number) : EMPTY_VALUE}
              </span>
              {hasValue && unit ? <span className="text-sm text-text-muted">{unit}</span> : null}
              {!hasValue ? <span className="sr-only">Sem dados</span> : null}
            </p>
            {trend && trend.some((v) => v !== null) ? (
              <Sparkline data={trend} className="h-10 w-24 shrink-0" />
            ) : null}
          </div>
          {comparison ? <DeltaLine comparison={comparison} /> : null}
          {description ? <p className="text-xs text-text-muted">{description}</p> : null}
        </>
      )}
      {footer}
    </Card>
  )
}
