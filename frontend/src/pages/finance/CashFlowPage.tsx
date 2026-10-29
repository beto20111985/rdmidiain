/** Financeiro → Fluxo de caixa: entradas e saídas nas contas de caixa do ledger, por dia ou mês. */
import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { PageHeader } from '@/components/layout/page-meta'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { KpiCard } from '@/components/patterns/kpi-card'
import { ChartFrame, ChartLegend } from '@/components/charts/chart-frame'
import { TimeSeriesChart } from '@/components/charts/time-series-chart'
import { SERIES_DASH, useChartTheme } from '@/components/charts/chart-theme'
import { PeriodSelector } from '@/components/finance/period-selector'
import { formatIsoDate, formatIsoDayMonth, formatIsoMonth, periodLabel, usePeriodState } from '@/components/finance/period'
import { LiveStale } from '@/components/finance/live-stale'
import { qk } from '@/lib/queryKeys'
import { formatCents } from '@/lib/format'
import { financeApi } from './api'

export default function CashFlowPage() {
  const { period, setPeriod } = usePeriodState({ defaultPreset: '30' })
  const [sp, setSp] = useSearchParams()
  const granularity = sp.get('agrupar') === 'month' ? 'month' : 'day'
  const theme = useChartTheme()
  const q = useQuery({
    queryKey: qk.sub('finance', 'cash-flow', period.start, period.end, granularity),
    queryFn: () => financeApi.cashFlow(period.start, period.end, granularity),
  })
  const d = q.data
  const fmtPeriod = granularity === 'month' ? formatIsoMonth : formatIsoDate
  const rows = (d?.rows ?? []).map((r) => ({ ...r, inflow: r.inflow_cents / 100, outflow: r.outflow_cents / 100 }))
  const totalIn = d ? d.rows.reduce((a, r) => a + r.inflow_cents, 0) : null
  const totalOut = d ? d.rows.reduce((a, r) => a + r.outflow_cents, 0) : null

  return (
    <div className="flex flex-col gap-6">
      <PageHeader title="Fluxo de caixa" description="Contas de caixa do ledger (CloudPayPag e externas)" breadcrumbs={[{ label: 'Financeiro', to: '/financeiro' }, { label: 'Fluxo de caixa' }]} />
      <div className="flex flex-wrap items-end gap-6">
        <PeriodSelector value={period} onChange={setPeriod} />
        <div className="flex flex-col gap-2">
          <span id="cf-gran" className="text-sm font-semibold text-text">
            Agrupar por
          </span>
          <ToggleGroup
            type="single"
            value={granularity}
            aria-labelledby="cf-gran"
            onValueChange={(v) =>
              v &&
              setSp(
                (prev) => {
                  const n = new URLSearchParams(prev)
                  if (v === 'month') n.set('agrupar', 'month')
                  else n.delete('agrupar')
                  return n
                },
                { replace: true },
              )
            }
          >
            <ToggleGroupItem value="day">Dia</ToggleGroupItem>
            <ToggleGroupItem value="month">Mês</ToggleGroupItem>
          </ToggleGroup>
        </div>
      </div>
      <LiveStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />
      <section aria-label="Resumo do caixa" className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4 xl:gap-6">
        <KpiCard label="Saldo inicial" value={d?.opening_balance_cents} format={formatCents} loading={q.isLoading} emphasis="secondary" />
        <KpiCard label="Entradas" value={totalIn} format={formatCents} loading={q.isLoading} emphasis="secondary" />
        <KpiCard label="Saídas" value={totalOut} format={formatCents} loading={q.isLoading} emphasis="secondary" />
        <KpiCard label="Saldo final" value={d?.closing_balance_cents} format={formatCents} loading={q.isLoading} emphasis="secondary" />
      </section>
      <ChartFrame
        title="Entradas e saídas"
        description={periodLabel(period)}
        summary={
          d
            ? `Entradas de ${formatCents(totalIn)} e saídas de ${formatCents(totalOut)}; saldo passou de ${formatCents(d.opening_balance_cents)} para ${formatCents(d.closing_balance_cents)}.`
            : 'Sem dados.'
        }
        loading={q.isLoading}
        error={q.error}
        onRetry={() => void q.refetch()}
        empty={!q.isLoading && !q.error && rows.length === 0}
        emptyText="Nenhuma movimentação de caixa neste período."
        table={{
          caption: 'Fluxo de caixa',
          columns: [
            { key: 'period', label: granularity === 'month' ? 'Mês' : 'Dia', render: (r) => fmtPeriod(r.period) },
            { key: 'in', label: 'Entradas', numeric: true, render: (r) => formatCents(r.inflow_cents) },
            { key: 'out', label: 'Saídas', numeric: true, render: (r) => formatCents(r.outflow_cents) },
            { key: 'net', label: 'Líquido', numeric: true, render: (r) => formatCents(r.net_cents) },
            { key: 'bal', label: 'Saldo', numeric: true, render: (r) => formatCents(r.balance_cents) },
          ],
          rows: d?.rows ?? [],
        }}
        legend={
          <ChartLegend
            items={[
              { label: 'Entradas', color: theme.series[0], dash: SERIES_DASH[0] },
              { label: 'Saídas', color: theme.series[1], dash: SERIES_DASH[1] },
            ]}
          />
        }
      >
        <TimeSeriesChart
          data={rows.map((r) => ({ period: r.period, inflow: r.inflow, outflow: r.outflow }))}
          xKey="period"
          variant="line"
          series={[
            { key: 'inflow', label: 'Entradas' },
            { key: 'outflow', label: 'Saídas' },
          ]}
          formatX={(v) => (granularity === 'month' ? formatIsoMonth(String(v)) : formatIsoDayMonth(v))}
          formatValue={(v) => formatCents(Math.round(v * 100))}
          formatTick={(v) => `R$ ${Math.round(v).toLocaleString('pt-BR')}`}
        />
      </ChartFrame>
    </div>
  )
}
