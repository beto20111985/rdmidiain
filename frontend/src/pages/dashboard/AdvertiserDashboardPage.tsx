/**
 * Minhas campanhas → Visão geral (P001 §102; ETAPA2 #749–#778; REQ-DSH-007/008).
 * Campanhas, anúncios, faturas, telas contratadas (QUANTIDADE), período, custo, aprovação e
 * comprovantes; regiões que exibiram apenas por UF — nunca nome/endereço de estabelecimento.
 * Sem reproduções em tempo real e sem audiência (exibição ≠ pessoa).
 */
import { Link } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Download, Megaphone, PlusCircle } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { KpiCard } from '@/components/patterns/kpi-card'
import { EmptyState, ErrorState, PageSkeleton, UpdatedAt } from '@/components/patterns/states'
import { Can } from '@/components/auth/guards'
import { StatusPill } from '@/components/finance/status-pill'
import { CAMPAIGN_STATUS, PAYMENT_STATUS } from '@/components/finance/labels'
import { formatIsoDate, periodLabel, usePeriodState } from '@/components/finance/period'
import { PeriodSelector } from '@/components/finance/period-selector'
import { useContextOrgId } from '@/components/finance/hooks'
import { useDownload } from '@/components/finance/use-download'
import { LiveStale } from '@/components/finance/live-stale'
import { useIsMobile } from '@/hooks/useMediaQuery'
import { useCan } from '@/hooks/usePermissions'
import { qk } from '@/lib/queryKeys'
import { EMPTY_VALUE, formatCents, formatDateTime, formatNumber } from '@/lib/format'
import { apiPath } from '@/pages/reports/api'
import { dashboardApi, type AdvertiserDashboard } from './api'

function period(c: { start_date: string | null; end_date: string | null }) {
  if (!c.start_date && !c.end_date) return EMPTY_VALUE
  return `${formatIsoDate(c.start_date)} a ${formatIsoDate(c.end_date)}`
}

function approval(c: AdvertiserDashboard['campaigns'][number]) {
  if (c.approval.rejection_reason) return <span className="text-danger">Rejeitada: {c.approval.rejection_reason}</span>
  if (c.approval.approved_at) return `Aprovada em ${formatDateTime(c.approval.approved_at)}`
  return <span className="text-text-muted">{EMPTY_VALUE}</span>
}

function CampaignsCard({ d }: { d: AdvertiserDashboard }) {
  const isMobile = useIsMobile()
  if (!d.campaigns.length) {
    return (
      <Card>
        <EmptyState
          size="compact"
          headingLevel="h3"
          icon={Megaphone}
          title="Você ainda não tem campanhas"
          description="Escolha pacote, região e período e siga para o pagamento."
          action={
            <Can permission="campaigns.manage">
              <Button asChild>
                <Link to="/anunciante/campanhas/nova">Criar campanha</Link>
              </Button>
            </Can>
          }
        />
      </Card>
    )
  }
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Suas campanhas</CardTitle>
          <CardDescription>Telas contratadas informadas por quantidade; locais não são divulgados.</CardDescription>
        </div>
      </CardHeader>
      {isMobile ? (
        <ul className="m-0 flex list-none flex-col divide-y divide-border p-0" aria-label="Suas campanhas">
          {d.campaigns.map((c) => (
            <li key={c.id} className="flex flex-col gap-1.5 py-3 first:pt-0 last:pb-0">
              <Link to={`/anunciante/campanhas/${c.id}`} className="font-semibold text-link underline-offset-4 hover:underline">
                {c.name}
              </Link>
              <StatusPill map={CAMPAIGN_STATUS} value={c.status} className="self-start" />
              <span className="text-xs text-text-muted">
                {period(c)} · {c.contracted_screens !== null ? `${formatNumber(c.contracted_screens)} telas` : 'telas: —'} · {formatCents(c.cost_cents)}
              </span>
              <span className="text-xs">{approval(c)}</span>
            </li>
          ))}
        </ul>
      ) : (
        <Table>
          <caption className="sr-only">Suas campanhas</caption>
          <TableHeader>
            <TableRow className="h-11 hover:bg-transparent">
              <TableHead scope="col">Campanha</TableHead>
              <TableHead scope="col">Situação</TableHead>
              <TableHead scope="col">Período</TableHead>
              <TableHead scope="col" numeric>
                Telas contratadas
              </TableHead>
              <TableHead scope="col" numeric>
                Custo
              </TableHead>
              <TableHead scope="col">Aprovação</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {d.campaigns.map((c) => (
              <TableRow key={c.id}>
                <TableCell>
                  <Link to={`/anunciante/campanhas/${c.id}`} className="font-semibold text-link underline-offset-4 hover:underline">
                    {c.name}
                  </Link>
                </TableCell>
                <TableCell>
                  <StatusPill map={CAMPAIGN_STATUS} value={c.status} />
                </TableCell>
                <TableCell className="tabular-nums">{period(c)}</TableCell>
                <TableCell numeric>{c.contracted_screens !== null ? formatNumber(c.contracted_screens) : EMPTY_VALUE}</TableCell>
                <TableCell numeric>{formatCents(c.cost_cents)}</TableCell>
                <TableCell className="text-sm">{approval(c)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </Card>
  )
}

export default function AdvertiserDashboardPage() {
  const orgId = useContextOrgId()
  const can = useCan()
  const dl = useDownload()
  const { period, setPeriod } = usePeriodState({ defaultPreset: '30' })
  const q = useQuery({
    queryKey: qk.sub('dashboard', 'advertiser', orgId ?? '', period.start, period.end),
    queryFn: () => dashboardApi.advertiser(orgId as string, { start: period.start, end: period.end }),
    enabled: !!orgId,
    placeholderData: keepPreviousData,
  })

  const actions = (
    <Can permission="campaigns.manage">
      <Button asChild>
        <Link to="/anunciante/campanhas/nova">
          <PlusCircle aria-hidden="true" />
          Nova campanha
        </Link>
      </Button>
    </Can>
  )

  if (!orgId) {
    return (
      <>
        <PageHeader title="Minhas campanhas" />
        <EmptyState icon={Megaphone} title="Selecione a empresa anunciante" description="Use o seletor de contexto para entrar em “Minhas campanhas”." />
      </>
    )
  }
  if (q.isLoading) return <PageSkeleton label="Carregando suas campanhas…" />
  if (q.error || !q.data) {
    return (
      <>
        <PageHeader title="Minhas campanhas" actions={actions} />
        <PeriodSelector value={period} onChange={setPeriod} />
        <ErrorState error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />
      </>
    )
  }
  const d = q.data

  return (
    <div className="flex flex-col gap-6">
      <PageHeader title="Minhas campanhas" description="Campanhas, faturas e comprovantes" actions={actions} />
      <PeriodSelector value={period} onChange={setPeriod} />
      <div className="flex flex-col gap-1">
        <UpdatedAt at={d.generated_at} />
        <p className="m-0 text-xs text-text-muted">
          {periodLabel(period)}: campanhas com vigência no período, regiões que exibiram e comprovantes do período.
        </p>
      </div>
      <LiveStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />

      <section aria-label="Indicadores" className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4 xl:gap-6">
        <KpiCard label="Campanhas ativas" value={d.kpis.campaigns_active} />
        <KpiCard label="Aguardando aprovação" value={d.kpis.campaigns_awaiting_approval} description="Em análise pela RD." />
        <KpiCard label="Campanhas" value={d.kpis.campaigns_total} emphasis="secondary" description="Total, em qualquer situação." />
        <KpiCard label="Anúncios" value={d.kpis.creatives_total} emphasis="secondary" description="Criativos cadastrados (ID RD-XXXXXX)." />
      </section>

      <CampaignsCard d={d} />

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-12 xl:gap-6">
        <Card className="xl:col-span-4">
          <CardHeader>
            <div className="flex flex-col gap-1">
              <CardTitle>Regiões que exibiram</CardTitle>
              <CardDescription>Telas com exibição válida no período, por estado (UF).</CardDescription>
            </div>
          </CardHeader>
          {d.regions_exhibited.length ? (
            <ul className="m-0 flex list-none flex-col gap-2 p-0 text-sm">
              {d.regions_exhibited.map((r) => (
                <li key={r.uf ?? 'sem-uf'} className="flex items-center justify-between gap-3">
                  <span>{r.uf ?? 'Sem UF'}</span>
                  <strong className="tabular-nums">
                    {formatNumber(r.screens)} {r.screens === 1 ? 'tela' : 'telas'}
                  </strong>
                </li>
              ))}
            </ul>
          ) : (
            <p className="m-0 text-sm text-text-muted">Nenhuma exibição válida registrada ainda.</p>
          )}
        </Card>

        <Card className="xl:col-span-4">
          <CardHeader>
            <CardTitle>Faturas recentes</CardTitle>
            {can('payments.view') ? (
              <Link to="/anunciante/faturas" className="text-sm font-semibold text-link underline-offset-4 hover:underline">
                Ver todas
              </Link>
            ) : null}
          </CardHeader>
          {d.invoices.length ? (
            <ul className="m-0 flex list-none flex-col divide-y divide-border p-0">
              {d.invoices.slice(0, 5).map((i) => (
                <li key={i.id} className="flex flex-wrap items-center justify-between gap-2 py-2.5 first:pt-0 last:pb-0">
                  <div className="flex flex-col">
                    <span className="font-semibold tabular-nums">{formatCents(i.amount_cents)}</span>
                    <span className="text-xs text-text-muted">{formatDateTime(i.created_at)}</span>
                  </div>
                  <StatusPill map={PAYMENT_STATUS} value={i.status} />
                </li>
              ))}
            </ul>
          ) : (
            <p className="m-0 text-sm text-text-muted">Nenhuma fatura ainda.</p>
          )}
        </Card>

        <Card className="xl:col-span-4">
          <CardHeader>
            <CardTitle>Comprovantes de veiculação</CardTitle>
            {can('reports.view') ? (
              <Link to="/anunciante/comprovantes" className="text-sm font-semibold text-link underline-offset-4 hover:underline">
                Ver todos
              </Link>
            ) : null}
          </CardHeader>
          {d.proofs.length ? (
            <ul className="m-0 flex list-none flex-col divide-y divide-border p-0">
              {d.proofs.slice(0, 5).map((p) => (
                <li key={p.id} className="flex flex-wrap items-center justify-between gap-2 py-2.5 first:pt-0 last:pb-0">
                  <div className="flex flex-col">
                    <span className="font-mono text-sm">{p.code}</span>
                    <span className="text-xs text-text-muted">
                      {formatIsoDate(p.period_start)} a {formatIsoDate(p.period_end)} · {formatNumber(p.total_valid_plays)} exibições válidas
                    </span>
                  </div>
                  <Button
                    variant="secondary"
                    loading={dl.isPending(p.id)}
                    loadingText="Baixando…"
                    aria-label={`Baixar comprovante ${p.code} em PDF`}
                    onClick={() => void dl.run(p.id, apiPath(p.pdf_url), { fallbackFilename: `comprovante-${p.code}.pdf` })}
                  >
                    <Download aria-hidden="true" />
                    PDF
                  </Button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="m-0 text-sm text-text-muted">Nenhum comprovante emitido ainda.</p>
          )}
        </Card>
      </div>

      {d.notes.length ? (
        <ul className="m-0 flex list-none flex-col gap-1 p-0 text-xs text-text-muted">
          {d.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}
