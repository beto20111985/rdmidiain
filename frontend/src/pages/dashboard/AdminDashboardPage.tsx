/**
 * Visão geral RD (DS §12; P001 §100; ETAPA2 #704–#720).
 * KPIs reais de `/dashboards/admin` (telas online, campanhas ativas, exibições registradas,
 * anunciantes ativos), operação (desatualizadas, aguardando aprovação, mídias com erro,
 * players offline), alertas, capacidade disponível (W-INV), finanças do mês (receita
 * RECONHECIDA × recebida × a receber, comissão pendente, projeção com método) e distribuição
 * por estado. Exibições no período vêm do relatório "Exibições por dia" (proof-of-play
 * válido). Exibição ≠ pessoa: nenhuma audiência é exibida aqui.
 * Sem mapa, cidade, top locais/campanhas e sem taxa de ocupação do inventário (REQ-DSH-003; E2 #717);
 * capacidade é exibida só como tempo LIVRE (E2 #718).
 */
import { Link } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, PlusCircle } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { KpiCard } from '@/components/patterns/kpi-card'
import { ErrorState, PageSkeleton, UpdatedAt } from '@/components/patterns/states'
import { ChartFrame, ChartLegend } from '@/components/charts/chart-frame'
import { TimeSeriesChart } from '@/components/charts/time-series-chart'
import { BarChart } from '@/components/charts/bar-chart'
import { useChartTheme } from '@/components/charts/chart-theme'
import { Can } from '@/components/auth/guards'
import { PeriodSelector } from '@/components/finance/period-selector'
import { fillDailySeries, formatIsoDate, formatIsoDayMonth, periodLabel, usePeriodState, type Period } from '@/components/finance/period'
import { StatusPill } from '@/components/finance/status-pill'
import { CAMPAIGN_STATUS } from '@/components/finance/labels'
import { LiveStale } from '@/components/finance/live-stale'
import { useCan } from '@/hooks/usePermissions'
import { qk } from '@/lib/queryKeys'
import { isApiError } from '@/lib/errors'
import { EMPTY_VALUE, formatCents, formatNumber } from '@/lib/format'
import type { Permission } from '@/lib/permissions'
import { reportsApi, sumColumn } from '@/pages/reports/api'
import { dashboardApi } from './api'

const ALERT_LINK: Record<string, { to: string; perm: Permission }> = {
  telas_offline_prolongado: { to: '/telas', perm: 'screens.view' },
  midias_com_erro: { to: '/biblioteca', perm: 'media.view' },
  campanhas_aguardando_aprovacao: { to: '/campanhas/aprovacoes', perm: 'campaigns.approve' },
  chamados_sla_estourado: { to: '/suporte', perm: 'support.view' },
  telas_desatualizadas: { to: '/telas', perm: 'screens.view' },
}

function ExhibitionsChart({ period }: { period: Period }) {
  const theme = useChartTheme()
  const q = useQuery({
    queryKey: qk.sub('dashboard', 'exhibitions-by-day', period.start, period.end),
    queryFn: () => reportsApi.data('exibicoes_por_dia', { start: period.start, end: period.end, audience: 'EQUIPE' }),
  })
  const rows = q.data?.rows ?? []
  const total = sumColumn(q.data?.rows, 'valid')
  const data = fillDailySeries(
    rows.map((r) => ({ day: String(r.day ?? ''), valid: typeof r.valid === 'number' ? r.valid : null })),
    period.start,
    period.end,
  )
  const peak = data.reduce<{ day: string; valid: number } | null>(
    (acc, r) => (r.valid !== null && (!acc || r.valid > acc.valid) ? { day: r.day, valid: r.valid } : acc),
    null,
  )
  return (
    <ChartFrame
        title="Exibições registradas ao longo do tempo"
        description={`${periodLabel(period)} · fuso America/Sao_Paulo · somente proof-of-play válido`}
        summary={
          total === null
            ? 'Sem dados.'
            : `${formatNumber(total)} exibições válidas no período${peak ? `; pico de ${formatNumber(peak.valid)} em ${formatIsoDate(peak.day)}` : ''}. Programação planejada não conta como exibição.`
        }
        loading={q.isLoading}
        error={q.error}
        onRetry={() => void q.refetch()}
        empty={!q.isLoading && !q.error && rows.length === 0}
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
        <TimeSeriesChart
          data={data}
          xKey="day"
          series={[{ key: 'valid', label: 'Exibições válidas' }]}
          formatX={(v) => formatIsoDayMonth(v)}
          formatValue={(v) => `${formatNumber(v)} exibições`}
        />
      </ChartFrame>
  )
}

function CapacityCard() {
  const q = useQuery({ queryKey: qk.sub('dashboard', 'capacity'), queryFn: dashboardApi.capacity, retry: false })
  const forbidden = isApiError(q.error) && (q.error.status === 403 || q.error.status === 404)
  const c = q.data
  return (
    <KpiCard
      label="Capacidade disponível hoje"
      value={forbidden ? null : c?.free_units}
      loading={q.isLoading}
      emphasis="secondary"
      unit={c ? `unidades de ${c.unit_seconds} s livres` : undefined}
      description={
        forbidden
          ? 'Disponível somente para a equipe com escopo nacional.'
          : q.error
            ? 'Não foi possível calcular a capacidade.'
            : c
              ? `${formatNumber(Math.floor(c.free_seconds / 3600))} h ${formatNumber(Math.floor((c.free_seconds % 3600) / 60))} min livres hoje em ${formatNumber(c.screens)} telas vendáveis${c.truncated ? ' · cálculo limitado às primeiras 500 telas' : ''}.`
              : undefined
      }
    />
  )
}

function EndingCampaigns() {
  const q = useQuery({ queryKey: qk.sub('campaigns', 'ending-soon'), queryFn: dashboardApi.endingCampaigns })
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Campanhas próximas do encerramento</CardTitle>
          <CardDescription>Ativas, ordenadas pela data final.</CardDescription>
        </div>
      </CardHeader>
      {q.isLoading ? (
        <p className="text-sm text-text-muted" role="status">
          Carregando…
        </p>
      ) : q.error ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} size="compact" />
      ) : !q.data?.items.length ? (
        <p className="text-sm text-text-muted">Nenhuma campanha ativa no momento.</p>
      ) : (
        <ul className="m-0 flex list-none flex-col divide-y divide-border p-0">
          {q.data.items.map((c) => (
            <li key={c.id} className="flex flex-wrap items-center justify-between gap-2 py-3 first:pt-0 last:pb-0">
              <div className="flex min-w-0 flex-col">
                <Link to={`/campanhas/${c.id}`} className="truncate font-semibold text-link underline-offset-4 hover:underline">
                  {c.name}
                </Link>
                <span className="text-xs text-text-muted">Termina em {formatIsoDate(c.end_date)}</span>
              </div>
              <StatusPill map={CAMPAIGN_STATUS} value={c.status} />
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}

export default function AdminDashboardPage() {
  const can = useCan()
  const theme = useChartTheme()
  const { period, setPeriod } = usePeriodState({ defaultPreset: '30' })
  const q = useQuery({
    queryKey: qk.sub('dashboard', 'admin', period.start, period.end),
    queryFn: () => dashboardApi.admin({ start: period.start, end: period.end }),
    placeholderData: keepPreviousData,
  })
  const d = q.data

  const actions = (
    <Can permission="campaigns.manage">
      <Button asChild>
        <Link to="/campanhas/nova">
          <PlusCircle aria-hidden="true" />
          Criar campanha
        </Link>
      </Button>
    </Can>
  )

  if (q.isLoading) {
    return (
      <>
        <PageHeader title="Visão geral" actions={actions} />
        <PageSkeleton label="Carregando visão geral…" />
      </>
    )
  }
  if (q.error || !d) {
    return (
      <>
        <PageHeader title="Visão geral" actions={actions} />
        <PeriodSelector value={period} onChange={setPeriod} />
        <ErrorState error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />
      </>
    )
  }

  const states = d.distribution_by_state.map((s) => ({ label: s.uf ?? 'Sem UF', value: s.screens, online: s.online, plays: s.exhibitions_period ?? null }))
  const pLabel = periodLabel(period)
  const totalStates = states.reduce((a, s) => a + s.value, 0)
  const f = d.finance

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Visão geral"
        description="Operação, exibições e finanças da RD Mídia Indoor"
        actions={actions}
      />
      <PeriodSelector value={period} onChange={setPeriod} />
      <div className="flex flex-col gap-1">
        <UpdatedAt at={d.generated_at} />
        {d.period?.note ? <p className="m-0 text-xs text-text-muted">{d.period.note}</p> : null}
      </div>
      <LiveStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />
      {d.scope.cities ? (
        <p className="m-0 text-sm text-text-muted">Escopo regional: {d.scope.cities.join(', ')}.</p>
      ) : null}

      <section aria-label="Indicadores principais" className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4 xl:gap-6">
        <KpiCard
          label="Telas online"
          value={d.kpis.screens_online}
          unit={`de ${formatNumber(d.kpis.screens_total)}`}
          description="Com comunicação ativa agora. Online não garante reprodução."
        />
        <KpiCard label="Campanhas ativas" value={d.kpis.campaigns_active} description="Publicadas, na vigência e não pausadas." />
        <KpiCard
          label="Exibições registradas no período"
          value={d.kpis.exhibitions_period ?? null}
          description={`${pLabel}. Hoje: ${formatNumber(d.kpis.exhibitions_today)}. Proof-of-play válido; exibição não é pessoa.`}
        />
        <KpiCard
          label="Anunciantes ativos"
          value={d.kpis.advertisers_active}
          description={`Com ao menos uma campanha ativa agora. Exibiram no período: ${formatNumber(d.kpis.advertisers_exhibited_period ?? null)}.`}
        />
      </section>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-12 xl:gap-6">
        <div className="xl:col-span-8">
          {can('reports.view') ? (
            <ExhibitionsChart period={period} />
          ) : (
            <Card>
              <CardTitle>Exibições ao longo do tempo</CardTitle>
              <p className="text-sm text-text-muted">Seu perfil não inclui relatórios (reports.view).</p>
            </Card>
          )}
        </div>
        <div className="xl:col-span-4">
          <ChartFrame
            title="Distribuição por estado"
            description={`Telas instaladas por UF · exibições no período`}
            summary={
              states.length
                ? `${formatNumber(totalStates)} telas em ${states.length} ${states.length === 1 ? 'estado' : 'estados'}; ${states
                    .slice(0, 3)
                    .map((s) => `${s.label}: ${formatNumber(s.value)}`)
                    .join(', ')}.`
                : 'Nenhuma tela cadastrada.'
            }
            empty={states.length === 0}
            emptyText="Nenhuma tela cadastrada."
            table={{
              caption: 'Telas por estado',
              columns: [
                { key: 'uf', label: 'UF', render: (r) => r.label },
                { key: 'screens', label: 'Telas', numeric: true, render: (r) => formatNumber(r.value) },
                { key: 'online', label: 'Online', numeric: true, render: (r) => formatNumber(r.online) },
                { key: 'plays', label: 'Exibições no período', numeric: true, render: (r) => formatNumber(r.plays) },
              ],
              rows: states,
            }}
            legend={<ChartLegend items={[{ label: 'Telas', color: theme.series[0], kind: 'swatch' }]} />}
          >
            <BarChart data={states.map((s) => ({ label: s.label, value: s.value }))} formatValue={(v) => `${formatNumber(v)} telas`} />
          </ChartFrame>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-12 xl:gap-6">
        <section aria-label="Operação" className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:col-span-8 xl:gap-6">
          <KpiCard label="Players offline" value={d.kpis.players_offline} emphasis="secondary" unit={`de ${formatNumber(d.kpis.players_paired)} pareados`} />
          <KpiCard label="Telas desatualizadas" value={d.operations.screens_outdated} emphasis="secondary" description="Programação nova ainda não aplicada no player." />
          <KpiCard label="Campanhas aguardando aprovação" value={d.operations.campaigns_awaiting_approval} emphasis="secondary" />
          <KpiCard
            label="Mídias com erro no período"
            value={d.operations.media_errors_period ?? null}
            emphasis="secondary"
            description={`Últimos 30 dias: ${formatNumber(d.operations.media_errors_30d)}.`}
          />
          <KpiCard label="Telas offline há muito tempo" value={d.operations.screens_offline_long} emphasis="secondary" description="Acima do limite de dias configurado para alerta." />
          <CapacityCard />
        </section>
        <div className="flex flex-col gap-4 xl:col-span-4 xl:gap-6">
          <Card>
            <CardHeader>
              <CardTitle>Alertas operacionais</CardTitle>
            </CardHeader>
            {d.alerts.length === 0 ? (
              <p className="flex items-center gap-2 text-sm text-text-muted">
                <CheckCircle2 aria-hidden="true" className="size-4 text-success" />
                Nenhum alerta no momento.
              </p>
            ) : (
              <ul className="m-0 flex list-none flex-col divide-y divide-border p-0">
                {d.alerts.map((a) => {
                  const link = ALERT_LINK[a.type]
                  const label = (
                    <span className="flex items-center gap-2">
                      <AlertTriangle aria-hidden="true" className="size-4 shrink-0 text-warning" />
                      {a.label}
                    </span>
                  )
                  return (
                    <li key={a.type} className="flex items-center justify-between gap-3 py-3 first:pt-0 last:pb-0">
                      {link && can(link.perm) ? (
                        <Link to={link.to} className="text-sm text-link underline-offset-4 hover:underline">
                          {label}
                        </Link>
                      ) : (
                        <span className="text-sm text-text">{label}</span>
                      )}
                      <Badge tone="warning" className="tabular-nums">
                        {formatNumber(a.count)}
                      </Badge>
                    </li>
                  )
                })}
              </ul>
            )}
          </Card>
          {can('campaigns.view') ? <EndingCampaigns /> : null}
        </div>
      </div>

      {f ? (
        <section aria-labelledby="fin-title" className="flex flex-col gap-4">
          <div className="flex flex-col gap-1">
            <h2 id="fin-title" className="text-title-section text-text">
              Financeiro
            </h2>
            <p className="m-0 text-xs text-text-muted">
              Valores do ledger (conciliados). Receita reconhecida = serviço prestado no período; recebido = pagamentos confirmados no período; a
              receber = cobranças pendentes.
            </p>
          </div>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-5 xl:gap-6">
            <KpiCard
              label="Receita reconhecida"
              value={f.revenue_period_cents ?? null}
              format={formatCents}
              emphasis="secondary"
              description={`No período. Mês corrente: ${formatCents(f.revenue_month_cents)}.`}
            />
            <KpiCard
              label="Faturamento recebido"
              value={f.billing_period_cents ?? null}
              format={formatCents}
              emphasis="secondary"
              description={`No período. Mês corrente: ${formatCents(f.billing_month_cents)}.`}
            />
            <KpiCard label="A receber (contratado)" value={f.pending_receivables_cents} format={formatCents} emphasis="secondary" />
            <KpiCard label="Comissão pendente" value={f.commission_pending_cents} format={formatCents} emphasis="secondary" description="Devida a estabelecimentos." />
            <KpiCard
              label="Projeção"
              value={f.revenue_projection_cents}
              format={formatCents}
              emphasis="secondary"
              description={f.projection_method || EMPTY_VALUE}
            />
          </div>
        </section>
      ) : null}
    </div>
  )
}
