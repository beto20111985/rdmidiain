/**
 * Financeiro → Contas a pagar (ETAPA2 #671–#679): vencimentos, atrasos e baixa.
 * A baixa (externa ou PIX OUT) é um PEDIDO de dupla aprovação — só os Admin Master solicitam.
 */
import * as React from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { ArrowUpFromLine } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { StatusPill, ToneBadge } from '@/components/finance/status-pill'
import { PAYABLE_STATUS } from '@/components/finance/labels'
import { formatIsoDate } from '@/components/finance/period'
import { useInstanceApprovalMode, useIsAdminMaster } from '@/components/finance/approval-utils'
import { LiveStale } from '@/components/finance/live-stale'
import { useTableState } from '@/hooks/useTableState'
import { qk } from '@/lib/queryKeys'
import { formatCents, formatDateTime, EMPTY_VALUE } from '@/lib/format'
import { financeApi, type Payable } from './api'
import { SettleDialog } from './expense-forms'

const FILTERS = [
  { value: 'OPEN', label: 'Em aberto' },
  { value: 'PAID', label: 'Pagas' },
  { value: 'CANCELLED', label: 'Canceladas' },
  { value: 'all', label: 'Todas' },
]

export default function PayablesPage() {
  const master = useIsAdminMaster()
  const single = useInstanceApprovalMode().isSingle
  const table = useTableState({ defaultPageSize: 25, filterKeys: ['status'] })
  const status = table.state.filters.status ?? 'OPEN'
  const [settling, setSettling] = React.useState<Payable | null>(null)
  const params = { page: table.state.page, page_size: table.state.pageSize, status: status === 'all' ? undefined : status }
  const q = useQuery({
    queryKey: qk.sub('finance', 'payables', params),
    queryFn: () => financeApi.payables(params),
    placeholderData: keepPreviousData,
  })

  const columns: DataTableColumn<Payable>[] = [
    {
      id: 'due',
      header: 'Vencimento',
      mobile: 'secondary',
      cell: (p) => (
        <div className="flex flex-col gap-1">
          <span className="tabular-nums">{formatIsoDate(p.due_date)}</span>
          {p.overdue ? <ToneBadge tone="danger">Vencida</ToneBadge> : null}
        </div>
      ),
    },
    { id: 'desc', header: 'Descrição', mobile: 'primary', cell: (p) => <span className="font-semibold">{p.description}</span> },
    { id: 'category', header: 'Categoria', mobile: 'field', cell: (p) => p.category },
    { id: 'supplier', header: 'Fornecedor', mobile: 'field', cell: (p) => p.supplier_name ?? EMPTY_VALUE },
    { id: 'amount', header: 'Valor', numeric: true, mobile: 'field', cell: (p) => formatCents(p.amount_cents) },
    { id: 'competence', header: 'Competência', mobile: 'field', cell: (p) => formatIsoDate(p.competence_date) },
    {
      id: 'status',
      header: 'Situação',
      mobile: 'badge',
      cell: (p) => (
        <div className="flex flex-col gap-1">
          <StatusPill map={PAYABLE_STATUS} value={p.status} />
          {p.settled_at ? <span className="text-xs text-text-muted">Paga em {formatDateTime(p.settled_at)}</span> : null}
        </div>
      ),
    },
  ]

  return (
    <div className="flex flex-col gap-6">
      <PageHeader title="Contas a pagar" description="Obrigações, vencimentos e baixas" breadcrumbs={[{ label: 'Financeiro', to: '/financeiro' }, { label: 'Contas a pagar' }]} />
      <LiveStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />
      {!master ? (
        <p className="m-0 text-sm text-text-muted">
          {single
            ? 'Baixas e alterações são solicitadas e autorizadas pelos Administradores Master.'
            : 'Baixas e alterações são solicitadas pelos Admin Master e exigem a aprovação do outro sócio.'}
        </p>
      ) : null}
      <Card>
        <DataTable
          caption="Contas a pagar"
          columns={columns}
          data={q.data}
          getRowId={(p) => p.id}
          controller={table}
          isLoading={q.isLoading}
          isFetching={q.isFetching}
          error={q.error}
          onRetry={() => void q.refetch()}
          filtersActive={status !== 'OPEN'}
          empty={{ title: 'Nenhuma conta em aberto.', description: 'Despesas aprovadas aparecem aqui até a baixa.', icon: ArrowUpFromLine }}
          toolbar={
            <div className="flex flex-col gap-2">
              <span id="payables-filter" className="text-sm font-semibold text-text">
                Situação
              </span>
              <ToggleGroup type="single" value={status} onValueChange={(v) => v && table.setFilter('status', v === 'OPEN' ? null : v)} aria-labelledby="payables-filter">
                {FILTERS.map((f) => (
                  <ToggleGroupItem key={f.value} value={f.value}>
                    {f.label}
                  </ToggleGroupItem>
                ))}
              </ToggleGroup>
            </div>
          }
          rowActions={
            master
              ? (p) =>
                  p.status === 'OPEN' ? (
                    <Button variant="secondary" onClick={() => setSettling(p)} aria-label={`Registrar pagamento de ${p.description}`}>
                      Registrar pagamento
                    </Button>
                  ) : null
              : undefined
          }
        />
      </Card>
      <SettleDialog payable={settling} onOpenChange={(o) => !o && setSettling(null)} />
    </div>
  )
}
