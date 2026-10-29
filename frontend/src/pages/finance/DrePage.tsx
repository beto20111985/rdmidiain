/** Financeiro → DRE (ledger `dre_group`): receitas, deduções, taxas, comissões e despesas do período. */
import { useQuery } from '@tanstack/react-query'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableFooter, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Skeleton } from '@/components/ui/skeleton'
import { KpiCard } from '@/components/patterns/kpi-card'
import { ErrorState } from '@/components/patterns/states'
import { PeriodSelector } from '@/components/finance/period-selector'
import { periodLabel, usePeriodState } from '@/components/finance/period'
import { DRE_SIGN_NOTE } from '@/components/finance/labels'
import { LiveStale } from '@/components/finance/live-stale'
import { qk } from '@/lib/queryKeys'
import { formatBps, formatCents } from '@/lib/format'
import { cn } from '@/lib/utils'
import { financeApi } from './api'

export default function DrePage() {
  const { period, setPeriod } = usePeriodState({ defaultPreset: '30' })
  const q = useQuery({ queryKey: qk.sub('finance', 'dre', period.start, period.end), queryFn: () => financeApi.dre(period.start, period.end) })
  const d = q.data

  return (
    <div className="flex flex-col gap-6">
      <PageHeader title="DRE" description="Demonstração do resultado por período (competência)" breadcrumbs={[{ label: 'Financeiro', to: '/financeiro' }, { label: 'DRE' }]} />
      <PeriodSelector value={period} onChange={setPeriod} />
      <LiveStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />
      <section aria-label="Resultado" className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4 xl:gap-6">
        <KpiCard label="Receita bruta" value={d?.gross_revenue_cents} format={formatCents} loading={q.isLoading} emphasis="secondary" />
        <KpiCard label="Receita líquida" value={d?.net_revenue_cents} format={formatCents} loading={q.isLoading} emphasis="secondary" />
        <KpiCard label="Resultado" value={d?.result_cents} format={formatCents} loading={q.isLoading} emphasis="secondary" />
        <KpiCard label="Margem" value={d?.margin_bps ?? null} format={(v) => formatBps(v, 1)} loading={q.isLoading} emphasis="secondary" description={d && d.margin_bps === null ? 'Sem receita líquida no período.' : 'Resultado ÷ receita líquida.'} />
      </section>
      <Card>
        <CardHeader>
          <div className="flex flex-col gap-1">
            <CardTitle>Demonstrativo · {periodLabel(period)}</CardTitle>
            <CardDescription>{DRE_SIGN_NOTE}</CardDescription>
          </div>
        </CardHeader>
        {q.isLoading ? (
          <Skeleton className="h-64 w-full" />
        ) : q.error ? (
          <ErrorState error={q.error} onRetry={() => void q.refetch()} size="compact" />
        ) : d ? (
          <Table>
            <caption className="sr-only">DRE do período</caption>
            <TableHeader>
              <TableRow className="h-11 hover:bg-transparent">
                <TableHead scope="col">Linha</TableHead>
                <TableHead scope="col" numeric>
                  Valor
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {d.lines.map((l) => (
                <TableRow key={l.group}>
                  <TableCell>{l.label}</TableCell>
                  <TableCell numeric className={cn(l.amount_cents < 0 && 'text-danger')}>
                    {formatCents(l.amount_cents)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
            <TableFooter>
              <TableRow>
                <TableCell className="font-semibold">Resultado do período</TableCell>
                <TableCell numeric className="font-semibold">
                  {formatCents(d.result_cents)}
                </TableCell>
              </TableRow>
            </TableFooter>
          </Table>
        ) : null}
      </Card>
    </div>
  )
}
