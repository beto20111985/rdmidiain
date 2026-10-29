/**
 * Financeiro → Comissões (P001 §55–§57, §86; REQ-COM-*): relatório por estabelecimento/
 * campanha/tela/dia/mês (só proof-of-play válido), explicação dos MODELOS A e B com exemplos do
 * backend, configuração do modelo/pool e exceções/ajustes por DUPLA APROVAÇÃO, saldo do
 * estabelecimento selecionado. Comissão de vendedores fica em CRM → Equipe comercial.
 */
import * as React from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { HandCoins, Settings2 } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Field, FieldLabel } from '@/components/ui/field'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { KpiCard } from '@/components/patterns/kpi-card'
import { ErrorState } from '@/components/patterns/states'
import { PeriodSelector } from '@/components/finance/period-selector'
import { formatIsoDate, formatIsoMonth, usePeriodState } from '@/components/finance/period'
import { StatusPill } from '@/components/finance/status-pill'
import { DetailList } from '@/components/finance/detail-list'
import { COMMISSION_STATUS } from '@/components/finance/labels'
import { useCampaignNames, useEstablishmentNames, useScreenNames } from '@/components/finance/hooks'
import { useIsAdminMaster } from '@/components/finance/approval-utils'
import { LiveStale } from '@/components/finance/live-stale'
import { useTableState } from '@/hooks/useTableState'
import { useCan } from '@/hooks/usePermissions'
import { qk } from '@/lib/queryKeys'
import { formatBps, formatCents, formatDateTime, formatNumber, EMPTY_VALUE } from '@/lib/format'
import { financeApi, type CommissionGroupBy, type CommissionRow, type CommissionSettings } from './api'
import { CommissionModelsCard } from './commission-models'
import { CommissionAdjustmentDialog, CommissionExceptionDialog, CommissionSettingDialog } from './commission-forms'
import { CommissionTermsSheet, type TermsTarget } from './commission-terms'

const GROUPS: Array<{ value: CommissionGroupBy; label: string }> = [
  { value: 'day', label: 'Dia' },
  { value: 'month', label: 'Mês' },
  { value: 'screen', label: 'Tela' },
  { value: 'campaign', label: 'Campanha' },
]

export default function CommissionsPage() {
  const can = useCan()
  const master = useIsAdminMaster()
  const ests = useEstablishmentNames()
  const screenName = useScreenNames()
  const campaignName = useCampaignNames()
  const { period, setPeriod } = usePeriodState({ defaultPreset: '30' })
  const table = useTableState({ defaultPageSize: 50, filterKeys: ['estabelecimento', 'agrupar'] })
  const estId = table.state.filters.estabelecimento ?? null
  const groupBy = (GROUPS.find((g) => g.value === table.state.filters.agrupar)?.value ?? 'day') as CommissionGroupBy
  const [settingOpen, setSettingOpen] = React.useState(false)
  const [exception, setException] = React.useState<CommissionSettings | null>(null)
  const [adjusting, setAdjusting] = React.useState<{ id: string; name: string } | null>(null)
  const [terms, setTerms] = React.useState<TermsTarget | null>(null)

  const params = { establishment_id: estId ?? undefined, start: period.start, end: period.end, group_by: groupBy }
  const q = useQuery({ queryKey: qk.sub('commissions', 'report', params), queryFn: () => financeApi.commissions(params) })
  const models = useQuery({ queryKey: qk.sub('commissions', 'models'), queryFn: financeApi.commissionModels, staleTime: 30 * 60 * 1000 })
  const settings = useQuery({
    queryKey: qk.sub('commissions', 'settings', estId ?? ''),
    queryFn: () => financeApi.commissionSettings(estId as string),
    enabled: !!estId && can('finance.view'),
  })
  const balance = useQuery({
    queryKey: qk.sub('commissions', 'balance', estId ?? ''),
    queryFn: () => financeApi.establishmentBalance(estId as string),
    enabled: !!estId,
  })

  const all = q.data?.items ?? []
  const start = (table.state.page - 1) * table.state.pageSize
  const pageData = q.data ? { items: all.slice(start, start + table.state.pageSize), total: all.length, page: table.state.page, page_size: table.state.pageSize } : undefined
  const totals = all.reduce((a, r) => ({ cents: a.cents + r.amount_cents, valid: a.valid + r.valid_plays, expected: a.expected + r.expected_plays }), { cents: 0, valid: 0, expected: 0 })

  const keyCol: DataTableColumn<CommissionRow> = {
    id: 'key',
    header: GROUPS.find((g) => g.value === groupBy)?.label ?? 'Grupo',
    mobile: 'primary',
    cell: (r) =>
      groupBy === 'day'
        ? formatIsoDate(r.local_day)
        : groupBy === 'month'
          ? formatIsoMonth(r.competence)
          : groupBy === 'screen'
            ? screenName(r.screen_id)
            : campaignName(r.campaign_id),
  }
  const columns: DataTableColumn<CommissionRow>[] = [
    keyCol,
    { id: 'est', header: 'Estabelecimento', mobile: 'secondary', cell: (r) => ests.nameOf(r.establishment_id) },
    { id: 'valid', header: 'Exibições válidas', numeric: true, mobile: 'field', cell: (r) => formatNumber(r.valid_plays) },
    { id: 'expected', header: 'Contratadas', numeric: true, mobile: 'field', cell: (r) => formatNumber(r.expected_plays) },
    { id: 'amount', header: 'Comissão', numeric: true, mobile: 'field', cell: (r) => formatCents(r.amount_cents) },
    { id: 'status', header: 'Situação', mobile: 'badge', cell: (r) => <StatusPill map={COMMISSION_STATUS} value={r.status} /> },
  ]
  const selectedName = estId ? ests.nameOf(estId) : ''

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Comissões"
        description="Estabelecimentos: apropriação diária por proof-of-play válido, fechamento mensal e liberação no dia 1"
        breadcrumbs={[{ label: 'Financeiro', to: '/financeiro' }, { label: 'Comissões' }]}
        actions={
          master ? (
            <Button variant="secondary" onClick={() => setSettingOpen(true)}>
              <Settings2 aria-hidden="true" />
              Alterar modelo/pool
            </Button>
          ) : undefined
        }
      />
      <LiveStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />
      <Card>
        <div className="flex flex-wrap items-end gap-6">
          <Field className="w-full max-w-sm">
            <FieldLabel>Estabelecimento</FieldLabel>
            <Select value={estId ?? 'all'} onValueChange={(v) => table.setFilter('estabelecimento', v === 'all' ? null : v)}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  <SelectItem value="all">Todos</SelectItem>
                  {ests.items.map((e) => (
                    <SelectItem key={e.id} value={e.id}>
                      {e.trade_name || e.name}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
          </Field>
          <div className="flex flex-col gap-2">
            <span id="comm-group" className="text-sm font-semibold text-text">
              Agrupar por
            </span>
            <ToggleGroup type="single" value={groupBy} aria-labelledby="comm-group" onValueChange={(v) => v && table.setFilter('agrupar', v === 'day' ? null : v)}>
              {GROUPS.map((g) => (
                <ToggleGroupItem key={g.value} value={g.value}>
                  {g.label}
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
          </div>
          <PeriodSelector value={period} onChange={setPeriod} />
        </div>
      </Card>

      <section aria-label="Totais do período" className="grid grid-cols-1 gap-4 md:grid-cols-3 xl:gap-6">
        <KpiCard label="Comissão no período" value={q.data ? totals.cents : null} format={formatCents} loading={q.isLoading} emphasis="secondary" />
        <KpiCard label="Exibições válidas" value={q.data ? totals.valid : null} loading={q.isLoading} emphasis="secondary" />
        <KpiCard
          label="Execução"
          value={q.data && totals.expected ? Math.floor((totals.valid * 10000) / totals.expected) : null}
          format={(v) => formatBps(v, 1)}
          loading={q.isLoading}
          emphasis="secondary"
          description="Válidas ÷ contratadas."
        />
      </section>

      {estId ? (
        <Card>
          <CardHeader>
            <div className="flex flex-col gap-1">
              <CardTitle>{selectedName}</CardTitle>
              <CardDescription>Saldo derivado do ledger e termos efetivos de comissão.</CardDescription>
            </div>
            {master ? (
              <div className="flex flex-wrap gap-2">
                <Button variant="secondary" disabled={!settings.data} onClick={() => settings.data && setException(settings.data)}>
                  Solicitar exceção
                </Button>
                <Button variant="secondary" onClick={() => setAdjusting({ id: estId, name: selectedName })}>
                  Ajuste manual
                </Button>
              </div>
            ) : null}
          </CardHeader>
          {balance.error ? (
            <ErrorState error={balance.error} onRetry={() => void balance.refetch()} size="compact" />
          ) : (
            <DetailList
              columns={3}
              items={[
                { label: 'Disponível', value: formatCents(balance.data?.available_cents ?? null), numeric: true },
                { label: 'Pendente (fechado)', value: formatCents(balance.data?.pending_closed_cents ?? null), numeric: true },
                { label: 'Mês em curso (provisório)', value: formatCents(balance.data?.pending_provisional_cents ?? null), numeric: true },
                { label: 'Em saque', value: formatCents(balance.data?.in_transit_cents ?? null), numeric: true },
                { label: 'Pago', value: formatCents(balance.data?.paid_cents ?? null), numeric: true },
                {
                  label: 'Saldo negativo',
                  value: balance.data?.withdrawals_blocked ? <span className="text-danger">{formatCents(balance.data.negative_balance_cents)} — saques bloqueados</span> : formatCents(0),
                  numeric: true,
                },
              ]}
            />
          )}
          {settings.data ? (
            <div className="flex flex-col gap-2">
              <h3 className="text-title-card text-text">Termos efetivos</h3>
              <DetailList
                columns={3}
                items={[
                  { label: 'Recebe comissão', value: settings.data.effective.receives_commission ? 'Sim' : 'Não' },
                  { label: 'Taxa', value: formatBps(settings.data.effective.rate_bps) },
                  { label: 'Peso', value: formatNumber(settings.data.effective.weight_milli / 1000, { maximumFractionDigits: 3 }) },
                  { label: 'Pagamento automático', value: settings.data.effective.auto_payout ? 'Sim (dia 1, a partir de R$ 1,00)' : 'Não' },
                  { label: 'Modelo da instância', value: settings.data.instance_model === 'A_POOL' ? 'A — pool fixo' : 'B — receita atribuída' },
                  { label: 'Pool da instância', value: formatBps(settings.data.instance_pool_bps) },
                ]}
              />
              {settings.data.history.length ? (
                <ul className="m-0 flex list-none flex-col gap-1 p-0 text-xs text-text-muted">
                  {settings.data.history.map((h) => (
                    <li key={h.id}>
                      Desde {formatDateTime(h.valid_from)}
                      {h.valid_to ? ` até ${formatDateTime(h.valid_to)}` : ''}: {h.receives_commission ? 'recebe' : 'não recebe'} · taxa {h.rate_bps !== null ? formatBps(h.rate_bps) : EMPTY_VALUE} · peso{' '}
                      {h.weight_milli !== null ? formatNumber(h.weight_milli / 1000, { maximumFractionDigits: 3 }) : EMPTY_VALUE}
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          ) : null}
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Relatório de comissões</CardTitle>
          {can('crm.view') ? (
            <Link to="/crm/equipe" className="text-sm font-semibold text-link underline-offset-4 hover:underline">
              Comissões de vendedores
            </Link>
          ) : null}
        </CardHeader>
        <DataTable
          caption="Relatório de comissões"
          columns={columns}
          data={pageData}
          getRowId={(r) => [r.local_day, r.competence, r.screen_id, r.campaign_id, r.establishment_id].join('|')}
          controller={table}
          isLoading={q.isLoading}
          isFetching={q.isFetching}
          error={q.error}
          onRetry={() => void q.refetch()}
          empty={{ title: 'Nenhuma comissão apropriada no período.', description: 'Comissões nascem de exibições válidas de campanhas pagas.', icon: HandCoins }}
          rowActions={
            groupBy === 'campaign'
              ? (r) =>
                  r.campaign_id ? (
                    <Button
                      variant="secondary"
                      onClick={() => setTerms({ campaignId: r.campaign_id as string, label: campaignName(r.campaign_id) })}
                      aria-label={`Termos por pagamento de ${campaignName(r.campaign_id)}`}
                    >
                      Termos por pagamento
                    </Button>
                  ) : null
              : undefined
          }
        />
        {groupBy !== 'campaign' ? (
          <p className="m-0 text-xs text-text-muted">Agrupe por campanha para ver os termos de cada pagamento (contratação inicial, renovação, alteração).</p>
        ) : null}
      </Card>

      <CommissionModelsCard />

      <CommissionSettingDialog open={settingOpen} onOpenChange={setSettingOpen} currentModel={models.data?.current_model ?? null} poolBps={models.data?.pool_bps ?? null} />
      <CommissionExceptionDialog settings={exception} establishmentName={selectedName} onOpenChange={(o) => !o && setException(null)} />
      <CommissionAdjustmentDialog establishment={adjusting} onOpenChange={(o) => !o && setAdjusting(null)} />
      <CommissionTermsSheet target={terms} onOpenChange={(o) => !o && setTerms(null)} />
    </div>
  )
}
