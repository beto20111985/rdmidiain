/**
 * Minha unidade → Visão geral (P001 §101; ETAPA2 #721–#748; REQ-DSH-005/006/011).
 * Suas telas e status, programação (QUANTIDADES de anúncios externos e conteúdos internos),
 * exibições, comissão, saldo e desempenho ("você deixou de ganhar ~R$ X" quando o backend
 * calcula a base). SEM dados comerciais do anunciante e SEM botão de saque: o saque fica
 * em Financeiro → Saques (P001 §86).
 */
import { Link } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { ArrowRight, Info, Store, TrendingDown } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { KpiCard } from '@/components/patterns/kpi-card'
import { StatusBadge } from '@/components/patterns/status-badge'
import { EmptyState, ErrorState, PageSkeleton, UpdatedAt } from '@/components/patterns/states'
import { ChartFrame, ChartLegend } from '@/components/charts/chart-frame'
import { TimeSeriesChart } from '@/components/charts/time-series-chart'
import { useChartTheme } from '@/components/charts/chart-theme'
import { PeriodSelector } from '@/components/finance/period-selector'
import { fillDailySeries, formatIsoDate, formatIsoDayMonth, periodLabel, usePeriodState, type Period } from '@/components/finance/period'
import { ToneBadge } from '@/components/finance/status-pill'
import { LiveStale } from '@/components/finance/live-stale'
import { useContextOrgId, useOrgEstablishments } from '@/components/finance/hooks'
import { useCan } from '@/hooks/usePermissions'
import { useIsMobile } from '@/hooks/useMediaQuery'
import { qk } from '@/lib/queryKeys'
import { DEFAULT_TIMEZONE, EMPTY_VALUE, formatBps, formatCents, formatDateTime, formatNumber } from '@/lib/format'
import { reportsApi, sumColumn } from '@/pages/reports/api'
import { dashboardApi, type EstablishmentDashboard } from './api'

const SYNC_LABEL: Record<string, { label: string; tone: 'success' | 'warning' | 'neutral' }> = {
  SINCRONIZADA: { label: 'Programação aplicada', tone: 'success' },
  SINCRONIZANDO: { label: 'Sincronizando', tone: 'warning' },
  NUNCA: { label: 'Nunca sincronizou', tone: 'neutral' },
}

function ExhibitionsByDay({ orgId, period }: { orgId: string; period: Period }) {
  const theme = useChartTheme()
  const q = useQuery({
    queryKey: qk.sub('dashboard', 'est-exhibitions', orgId, period.start, period.end),
    queryFn: () => reportsApi.data('exibicoes_por_dia', { start: period.start, end: period.end, audience: 'ESTABELECIMENTO', org_id: orgId }),
  })
  const data = fillDailySeries(
    (q.data?.rows ?? []).map((r) => ({ day: String(r.day ?? ''), valid: typeof r.valid === 'number' ? r.valid : null })),
    period.start,
    period.end,
  )
  const total = sumColumn(q.data?.rows, 'valid')
  return (
    <ChartFrame
        title="Exibições comprovadas nas suas telas"
        description={`${periodLabel(period)} · externos e internos · proof-of-play válido`}
        summary={total === null ? 'Sem dados.' : `${formatNumber(total)} exibições válidas no período (todas as suas telas).`}
        loading={q.isLoading}
        error={q.error}
        onRetry={() => void q.refetch()}
        empty={!q.isLoading && !q.error && (q.data?.rows.length ?? 0) === 0}
        emptyText="Nenhuma exibição registrada neste período."
        table={{
          caption: 'Exibições válidas por dia',
          columns: [
            { key: 'day', label: 'Dia', render: (r) => formatIsoDate(r.day) },
            { key: 'valid', label: 'Exibições válidas', numeric: true, render: (r) => formatNumber(r.valid) },
          ],
          rows: data,
        }}
        legend={<ChartLegend items={[{ label: 'Exibições válidas', color: theme.series[0] }]} />}
      >
        <TimeSeriesChart data={data} xKey="day" series={[{ key: 'valid', label: 'Exibições válidas' }]} formatX={(v) => formatIsoDayMonth(v)} formatValue={(v) => `${formatNumber(v)} exibições`} />
      </ChartFrame>
  )
}

function ScreensCard({ d, tzOf }: { d: EstablishmentDashboard; tzOf: (estId: string) => string }) {
  const isMobile = useIsMobile()
  const estName = (id: string) => d.establishments.find((e) => e.id === id)?.name ?? EMPTY_VALUE
  const multi = d.establishments.length > 1
  if (!d.screens.length) {
    return (
      <Card>
        <EmptyState size="compact" headingLevel="h3" title="Nenhuma tela instalada ainda" description="Quando a RD instalar e parear suas telas, elas aparecem aqui." />
      </Card>
    )
  }
  const status = (s: EstablishmentDashboard['screens'][number]) => (
    <div className="flex flex-col gap-1">
      <StatusBadge status={s.online ? 'online' : 'offline'} />
      {!s.online ? (
        <span className="text-xs text-text-muted">
          Sem comunicação. Último contato: {s.last_seen_at ? formatDateTime(s.last_seen_at, tzOf(s.establishment_id)) : 'nunca'}
        </span>
      ) : null}
    </div>
  )
  const sync = (s: EstablishmentDashboard['screens'][number]) => {
    const e = s.sync ? SYNC_LABEL[s.sync] : null
    return e ? <ToneBadge tone={e.tone}>{e.label}</ToneBadge> : <span className="text-text-muted">{EMPTY_VALUE}</span>
  }
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Suas telas</CardTitle>
          <CardDescription>Status de comunicação e sincronização da programação. Online não garante reprodução.</CardDescription>
        </div>
      </CardHeader>
      {isMobile ? (
        <ul className="m-0 flex list-none flex-col divide-y divide-border p-0" aria-label="Suas telas">
          {d.screens.map((s) => (
            <li key={s.id} className="flex flex-col gap-2 py-3 first:pt-0 last:pb-0">
              <span className="font-semibold">
                {s.name} <span className="font-mono text-xs text-text-muted">{s.code}</span>
              </span>
              {multi ? <span className="text-xs text-text-muted">{estName(s.establishment_id)}</span> : null}
              {status(s)}
              {sync(s)}
            </li>
          ))}
        </ul>
      ) : (
        <Table>
          <caption className="sr-only">Suas telas</caption>
          <TableHeader>
            <TableRow className="h-11 hover:bg-transparent">
              <TableHead scope="col">Tela</TableHead>
              {multi ? <TableHead scope="col">Unidade</TableHead> : null}
              <TableHead scope="col">Comunicação</TableHead>
              <TableHead scope="col">Programação</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {d.screens.map((s) => (
              <TableRow key={s.id}>
                <TableCell>
                  <div className="flex flex-col">
                    <span className="font-semibold">{s.name}</span>
                    <span className="font-mono text-xs text-text-muted">{s.code}</span>
                  </div>
                </TableCell>
                {multi ? <TableCell>{estName(s.establishment_id)}</TableCell> : null}
                <TableCell>{status(s)}</TableCell>
                <TableCell>{sync(s)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </Card>
  )
}

export default function EstablishmentDashboardPage() {
  const orgId = useContextOrgId()
  const can = useCan()
  const ests = useOrgEstablishments(orgId)
  const { period, setPeriod } = usePeriodState({ defaultPreset: '30' })
  const q = useQuery({
    queryKey: qk.sub('dashboard', 'establishment', orgId ?? '', period.start, period.end),
    queryFn: () => dashboardApi.establishment(orgId as string, { start: period.start, end: period.end }),
    enabled: !!orgId,
    placeholderData: keepPreviousData,
  })
  const tzOf = (estId: string) => ests.data?.items.find((e) => e.id === estId)?.timezone || DEFAULT_TIMEZONE

  if (!orgId) {
    return (
      <>
        <PageHeader title="Minha unidade" />
        <EmptyState icon={Store} title="Selecione a sua unidade" description="Use o seletor de contexto para entrar em “Minha unidade”." />
      </>
    )
  }
  if (q.isLoading) return <PageSkeleton label="Carregando a visão geral da unidade…" />
  if (q.error || !q.data) {
    return (
      <>
        <PageHeader title="Minha unidade" />
        <PeriodSelector value={period} onChange={setPeriod} />
        <ErrorState error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />
      </>
    )
  }
  const d = q.data
  const c = d.commission
  const title = d.establishments.length === 1 ? d.establishments[0].name : 'Minha unidade'
  const pLabel = periodLabel(period)
  const validP = c?.valid_plays_period ?? null
  const expectedP = c?.expected_plays_period ?? null

  return (
    <div className="flex flex-col gap-6">
      <PageHeader title={title} description="Suas telas, exibições e comissão" />
      <PeriodSelector value={period} onChange={setPeriod} />
      <div className="flex flex-col gap-1">
        <UpdatedAt at={d.generated_at} />
        {d.period?.note ? <p className="m-0 text-xs text-text-muted">{d.period.note}</p> : null}
      </div>
      <LiveStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />

      <section aria-label="Indicadores da unidade" className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4 xl:gap-6">
        <KpiCard label="Telas online" value={d.kpis.screens_online} unit={`de ${formatNumber(d.kpis.screens_total)}`} description={d.kpis.screens_offline ? `${d.kpis.screens_offline} sem comunicação agora.` : 'Todas comunicando agora.'} />
        <KpiCard
          label="Exibições no período"
          value={d.kpis.exhibitions_period ?? null}
          description={`${pLabel}. Anúncios: ${formatNumber(d.kpis.external_exhibitions_period ?? null)} · conteúdo da unidade: ${formatNumber(d.kpis.internal_exhibitions_period ?? null)}. Exibição não é pessoa.`}
        />
        <KpiCard
          label="Comissão no período"
          value={c?.commission_period_cents ?? null}
          format={formatCents}
          description={c ? `${formatNumber(validP)} exibições válidas de anúncios. Mês corrente: ${formatCents(c.commission_month_cents)}.` : 'Comissão não configurada para a unidade.'}
        />
        <KpiCard
          label="Saldo disponível"
          value={c?.available_cents ?? null}
          format={formatCents}
          description={c ? `Pendente (libera no dia 1): ${formatCents(c.pending_cents)}.` : undefined}
          footer={
            can('withdrawals.view') ? (
              <Link to="/unidade/financeiro/saques" className="inline-flex items-center gap-1 text-sm font-semibold text-link underline-offset-4 hover:underline">
                Saques ficam em Financeiro → Saques
                <ArrowRight aria-hidden="true" className="size-4" />
              </Link>
            ) : null
          }
        />
      </section>

      {c && (c.performance_bps !== null || c.estimated_lost_cents !== null) ? (
        <Card>
          <CardHeader>
            <div className="flex flex-col gap-1">
              <CardTitle>Desempenho no período</CardTitle>
              <CardDescription>Exibições comprovadas em relação às contratadas para as suas telas.</CardDescription>
            </div>
          </CardHeader>
          <p className="m-0 text-sm text-text">
            {c.performance_bps !== null ? (
              <>
                <strong className="tabular-nums">{formatBps(c.performance_bps, 1)}</strong> das exibições contratadas foram comprovadas (
                {formatNumber(validP)} de {formatNumber(expectedP)}).
              </>
            ) : (
              'Sem base de comparação neste período.'
            )}
          </p>
          {c.estimated_lost_cents !== null && c.estimated_lost_cents > 0 ? (
            <Alert variant="warning">
              <TrendingDown aria-hidden="true" />
              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <AlertTitle>Você deixou de ganhar cerca de {formatCents(c.estimated_lost_cents)} neste período</AlertTitle>
                <AlertDescription>
                  {c.estimated_lost_note ?? 'Estimativa por telas offline ou sem sincronizar.'} Mantenha as telas ligadas e conectadas.
                </AlertDescription>
              </div>
            </Alert>
          ) : null}
        </Card>
      ) : null}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-12 xl:gap-6">
        <div className="xl:col-span-8">{can('reports.view') ? <ExhibitionsByDay orgId={orgId} period={period} /> : <ScreensCard d={d} tzOf={tzOf} />}</div>
        <div className="flex flex-col gap-4 xl:col-span-4 xl:gap-6">
          <Card>
            <CardHeader>
              <CardTitle>Programação de hoje</CardTitle>
            </CardHeader>
            <ul className="m-0 flex list-none flex-col gap-3 p-0 text-sm">
              <li className="flex items-center justify-between gap-3">
                <span>Anúncios externos em exibição</span>
                <strong className="tabular-nums">{formatNumber(d.programming.external_ads_running_today)}</strong>
              </li>
              <li className="flex items-center justify-between gap-3">
                <span>Conteúdos da unidade (internos)</span>
                <strong className="tabular-nums">{formatNumber(d.programming.internal_contents_today)}</strong>
              </li>
            </ul>
            <p className="m-0 flex items-start gap-2 text-xs text-text-muted">
              <Info aria-hidden="true" className="mt-px size-4 shrink-0" />
              {d.programming.note}
            </p>
            {can('playlists.reorder') ? (
              <Link to="/unidade/programacao" className="text-sm font-semibold text-link underline-offset-4 hover:underline">
                Ordenar o conteúdo da unidade
              </Link>
            ) : null}
          </Card>
          {c ? (
            <Card>
              <CardHeader>
                <CardTitle>Comissão acumulada</CardTitle>
              </CardHeader>
              <p className="m-0 text-kpi-secondary tabular-nums">{formatCents(c.accumulated_cents)}</p>
              <p className="m-0 text-xs text-text-muted">Desde o início, incluindo o mês em curso (provisório).</p>
              {can('commissions.view') ? (
                <Link to="/unidade/comissoes" className="text-sm font-semibold text-link underline-offset-4 hover:underline">
                  Ver comissões por tela, dia e mês
                </Link>
              ) : null}
            </Card>
          ) : null}
        </div>
      </div>
      {can('reports.view') ? <ScreensCard d={d} tzOf={tzOf} /> : null}
    </div>
  )
}
