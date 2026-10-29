/**
 * Minha unidade → Comissões (P001 §86; ETAPA2 #727–#728; REQ-COM-013): saldo, comissão por
 * tela/dia/mês (somente exibições válidas), situação (competência aberta × fechada), extrato
 * da unidade e as regras do cálculo. Sem dados comerciais do anunciante (nem campanha/ID): o
 * agrupamento "Contratação" numera as contratações (1, 2…) e abre os termos por pagamento
 * (W-FIN 0006, visão ESTABLISHMENT — só a comissão da unidade).
 * Saques ficam em Financeiro → Saques.
 */
import * as React from 'react'
import { Link } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { ArrowRight, HandCoins, Store } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Button } from '@/components/ui/button'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { KpiCard } from '@/components/patterns/kpi-card'
import { EmptyState, ErrorState, PageSkeleton } from '@/components/patterns/states'
import { PeriodSelector } from '@/components/finance/period-selector'
import { formatIsoDate, formatIsoMonth, usePeriodState } from '@/components/finance/period'
import { StatusPill } from '@/components/finance/status-pill'
import { COMMISSION_STATUS, LEDGER_KIND_LABEL } from '@/components/finance/labels'
import { useContextOrgId, useScreenNames, useSelectedEstablishment } from '@/components/finance/hooks'
import { EstablishmentPicker } from '@/components/finance/establishment-picker'
import { LiveStale } from '@/components/finance/live-stale'
import { useTableState } from '@/hooks/useTableState'
import { useCan } from '@/hooks/usePermissions'
import { qk } from '@/lib/queryKeys'
import { formatBps, formatCents, formatNumber } from '@/lib/format'
import { cn } from '@/lib/utils'
import { financeApi, type CommissionGroupBy, type CommissionRow, type StatementItem } from './api'
import { CommissionModelsCard } from './commission-models'
import { CommissionTermsSheet, type TermsTarget } from './commission-terms'

const GROUPS: Array<{ value: CommissionGroupBy; label: string }> = [
  { value: 'day', label: 'Dia' },
  { value: 'month', label: 'Mês' },
  { value: 'screen', label: 'Tela' },
  { value: 'campaign', label: 'Contratação' },
]

function Statement({ orgId }: { orgId: string }) {
  const { period, setPeriod } = usePeriodState({ defaultPreset: '30', prefix: 'x_' })
  const table = useTableState({ defaultPageSize: 25, prefix: 'x_' })
  const params = { start: period.start, end: period.end, page: table.state.page, page_size: table.state.pageSize }
  const q = useQuery({
    queryKey: qk.sub('commissions', 'statement', orgId, params),
    queryFn: () => financeApi.orgStatement(orgId, params),
    placeholderData: keepPreviousData,
  })
  const cols: DataTableColumn<StatementItem>[] = [
    { id: 'date', header: 'Data', mobile: 'secondary', cell: (s) => formatIsoDate(s.competence_date) },
    { id: 'desc', header: 'Movimento', mobile: 'primary', cell: (s) => <span className="font-semibold">{LEDGER_KIND_LABEL[s.kind] ?? s.description}</span> },
    { id: 'amount', header: 'Valor', numeric: true, mobile: 'field', cell: (s) => <span className={cn(s.natural_amount_cents < 0 && 'text-danger')}>{formatCents(s.natural_amount_cents)}</span> },
  ]
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Extrato da unidade</CardTitle>
          <CardDescription>Movimentos de comissão, saques e ajustes no ledger.</CardDescription>
        </div>
      </CardHeader>
      <PeriodSelector value={period} onChange={setPeriod} />
      <DataTable
        caption="Extrato da unidade"
        columns={cols}
        data={q.data}
        getRowId={(s) => s.posting_id}
        controller={table}
        isLoading={q.isLoading}
        isFetching={q.isFetching}
        error={q.error}
        onRetry={() => void q.refetch()}
        empty={{ title: 'Nenhum movimento neste período.' }}
      />
    </Card>
  )
}

export default function EstablishmentCommissionsPage() {
  const orgId = useContextOrgId()
  const can = useCan()
  const { query: estQ, items, selected, select } = useSelectedEstablishment(orgId)
  const estId = selected?.id ?? null
  const screenName = useScreenNames(estId ? { establishment_id: estId } : {})
  const { period, setPeriod } = usePeriodState({ defaultPreset: '30' })
  const table = useTableState({ defaultPageSize: 25, filterKeys: ['agrupar'] })
  const groupBy = (GROUPS.find((g) => g.value === table.state.filters.agrupar)?.value ?? 'day') as CommissionGroupBy
  const [terms, setTerms] = React.useState<TermsTarget | null>(null)

  const balance = useQuery({
    queryKey: qk.sub('commissions', 'balance', estId ?? ''),
    queryFn: () => financeApi.establishmentBalance(estId as string),
    enabled: !!estId,
  })
  const params = { establishment_id: estId ?? undefined, start: period.start, end: period.end, group_by: groupBy }
  const q = useQuery({ queryKey: qk.sub('commissions', 'report', params), queryFn: () => financeApi.commissions(params), enabled: !!estId })

  if (!orgId) {
    return (
      <>
        <PageHeader title="Comissões" />
        <EmptyState icon={Store} title="Selecione a sua unidade" description="Use o seletor de contexto para entrar em “Minha unidade”." />
      </>
    )
  }
  if (estQ.isLoading) return <PageSkeleton label="Carregando comissões…" />
  if (estQ.error) return <ErrorState error={estQ.error} onRetry={() => void estQ.refetch()} />
  if (!estId) {
    return (
      <>
        <PageHeader title="Comissões" />
        <EmptyState icon={Store} title="Nenhum estabelecimento vinculado" />
      </>
    )
  }

  const all = q.data?.items ?? []
  const start = (table.state.page - 1) * table.state.pageSize
  const pageData = q.data ? { items: all.slice(start, start + table.state.pageSize), total: all.length, page: table.state.page, page_size: table.state.pageSize } : undefined
  // Contratações numeradas na ordem do relatório — o id da campanha nunca aparece para a unidade.
  const contractNo = new Map(all.map((r, i) => [r.campaign_id ?? '', i + 1]))
  const contractLabel = (r: CommissionRow) => `Contratação ${contractNo.get(r.campaign_id ?? '') ?? '—'}`
  const totals = all.reduce((a, r) => ({ cents: a.cents + r.amount_cents, valid: a.valid + r.valid_plays, expected: a.expected + r.expected_plays }), { cents: 0, valid: 0, expected: 0 })
  const b = balance.data
  const columns: DataTableColumn<CommissionRow>[] = [
    {
      id: 'key',
      header: GROUPS.find((g) => g.value === groupBy)?.label ?? 'Grupo',
      mobile: 'primary',
      cell: (r) =>
        groupBy === 'day'
          ? formatIsoDate(r.local_day)
          : groupBy === 'month'
            ? formatIsoMonth(r.competence)
            : groupBy === 'campaign'
              ? contractLabel(r)
              : screenName(r.screen_id),
    },
    { id: 'valid', header: 'Exibições válidas', numeric: true, mobile: 'field', cell: (r) => formatNumber(r.valid_plays) },
    { id: 'expected', header: 'Contratadas', numeric: true, mobile: 'field', cell: (r) => formatNumber(r.expected_plays) },
    { id: 'amount', header: 'Comissão', numeric: true, mobile: 'field', cell: (r) => formatCents(r.amount_cents) },
    { id: 'status', header: 'Situação', mobile: 'badge', cell: (r) => <StatusPill map={COMMISSION_STATUS} value={r.status} /> },
  ]

  return (
    <div className="flex flex-col gap-6">
      <PageHeader title="Comissões" description="Comissão sobre exibições válidas dos anúncios nas suas telas" breadcrumbs={[{ label: 'Minha unidade', to: '/unidade' }, { label: 'Comissões' }]} />
      <EstablishmentPicker items={items} selectedId={estId} onSelect={select} />
      <LiveStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />
      <section aria-label="Saldo de comissões" className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4 xl:gap-6">
        <KpiCard label="Disponível" value={b?.available_cents} format={formatCents} loading={balance.isLoading} />
        <KpiCard label="Pendente" value={b ? b.pending_closed_cents + b.pending_provisional_cents : null} format={formatCents} loading={balance.isLoading} emphasis="secondary" description="Libera no dia 1 após o fechamento do mês." />
        <KpiCard label="Acumulado" value={b?.accumulated_cents} format={formatCents} loading={balance.isLoading} emphasis="secondary" />
        <KpiCard
          label="Pago"
          value={b?.paid_cents}
          format={formatCents}
          loading={balance.isLoading}
          emphasis="secondary"
          footer={
            can('withdrawals.view') ? (
              <Link to="/unidade/financeiro/saques" className="inline-flex items-center gap-1 text-sm font-semibold text-link underline-offset-4 hover:underline">
                Financeiro → Saques
                <ArrowRight aria-hidden="true" className="size-4" />
              </Link>
            ) : null
          }
        />
      </section>
      {balance.error ? <ErrorState error={balance.error} onRetry={() => void balance.refetch()} size="compact" /> : null}
      <Card>
        <div className="flex flex-wrap items-end gap-6">
          <div className="flex flex-col gap-2">
            <span id="est-comm-group" className="text-sm font-semibold text-text">
              Agrupar por
            </span>
            <ToggleGroup type="single" value={groupBy} aria-labelledby="est-comm-group" onValueChange={(v) => v && table.setFilter('agrupar', v === 'day' ? null : v)}>
              {GROUPS.map((g) => (
                <ToggleGroupItem key={g.value} value={g.value}>
                  {g.label}
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
          </div>
          <PeriodSelector value={period} onChange={setPeriod} />
        </div>
        <p className="m-0 text-sm text-text">
          No período: <strong className="tabular-nums">{formatCents(q.data ? totals.cents : null)}</strong> sobre {formatNumber(q.data ? totals.valid : null)} exibições válidas
          {totals.expected ? ` (${formatBps(Math.floor((totals.valid * 10000) / totals.expected), 1)} das contratadas)` : ''}.
        </p>
        <DataTable
          caption="Comissões da unidade"
          columns={columns}
          data={pageData}
          getRowId={(r) => [r.local_day, r.competence, r.screen_id, r.campaign_id, r.establishment_id].join('|')}
          controller={table}
          isLoading={q.isLoading}
          isFetching={q.isFetching}
          error={q.error}
          onRetry={() => void q.refetch()}
          empty={{ title: 'Nenhuma comissão no período.', description: 'A comissão nasce das exibições válidas de anúncios pagos nas suas telas.', icon: HandCoins }}
          rowActions={
            groupBy === 'campaign'
              ? (r) =>
                  r.campaign_id ? (
                    <Button
                      variant="secondary"
                      onClick={() => setTerms({ campaignId: r.campaign_id as string, label: contractLabel(r) })}
                      aria-label={`Termos por pagamento da ${contractLabel(r)}`}
                    >
                      Termos por pagamento
                    </Button>
                  ) : null
              : undefined
          }
        />
      </Card>
      {orgId ? <Statement orgId={orgId} /> : null}
      <CommissionModelsCard showExamples={false} />
      <CommissionTermsSheet target={terms} onOpenChange={(o) => !o && setTerms(null)} />
    </div>
  )
}
