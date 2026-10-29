/**
 * Financeiro → Despesas (P001 §85, §91; REQ-FIN-005): lançamento e alteração de despesas
 * operacionais SEMPRE por pedido de dupla aprovação (o lançamento no ledger só ocorre quando
 * o outro Admin Master aprovar). Lista as despesas lançadas e os pedidos ainda pendentes.
 */
import * as React from 'react'
import { Link } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Pencil, PlusCircle, Receipt } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { StatusPill } from '@/components/finance/status-pill'
import { APPROVAL_STATUS, PAYABLE_STATUS } from '@/components/finance/labels'
import { formatIsoDate } from '@/components/finance/period'
import { useInstanceApprovalMode, useIsAdminMaster } from '@/components/finance/approval-utils'
import { personLabel } from '@/components/finance/names'
import { LiveStale } from '@/components/finance/live-stale'
import { useTableState } from '@/hooks/useTableState'
import { qk } from '@/lib/queryKeys'
import { formatCents, formatDateTime, EMPTY_VALUE } from '@/lib/format'
import { financeApi, type Payable } from './api'
import { ExpenseDialog } from './expense-forms'

export default function ExpensesPage() {
  const master = useIsAdminMaster()
  const single = useInstanceApprovalMode().isSingle
  const table = useTableState({ defaultPageSize: 25 })
  const [dialog, setDialog] = React.useState<{ open: boolean; payable: Payable | null }>({ open: false, payable: null })
  const params = { page: table.state.page, page_size: table.state.pageSize }
  const q = useQuery({
    queryKey: qk.sub('finance', 'payables', { ...params, all: true }),
    queryFn: () => financeApi.payables(params),
    placeholderData: keepPreviousData,
  })
  const pending = useQuery({
    queryKey: qk.sub('finance', 'approvals', 'pending-count'),
    queryFn: () => financeApi.approvals({ status: 'PENDING', page_size: 100 }),
  })
  const pendingExpenses = (pending.data?.items ?? []).filter((r) => r.action_type === 'EXPENSE_CREATE' || r.action_type === 'EXPENSE_UPDATE')

  const columns: DataTableColumn<Payable>[] = [
    { id: 'competence', header: 'Competência', mobile: 'secondary', cell: (p) => formatIsoDate(p.competence_date) },
    { id: 'desc', header: 'Descrição', mobile: 'primary', cell: (p) => <span className="font-semibold">{p.description}</span> },
    { id: 'category', header: 'Categoria', mobile: 'field', cell: (p) => p.category },
    { id: 'supplier', header: 'Fornecedor', mobile: 'field', cell: (p) => p.supplier_name ?? EMPTY_VALUE },
    { id: 'amount', header: 'Valor', numeric: true, mobile: 'field', cell: (p) => formatCents(p.amount_cents) },
    { id: 'due', header: 'Vencimento', mobile: 'field', cell: (p) => formatIsoDate(p.due_date) },
    { id: 'status', header: 'Situação', mobile: 'badge', cell: (p) => <StatusPill map={PAYABLE_STATUS} value={p.status} /> },
  ]

  const actions = master ? (
    <Button onClick={() => setDialog({ open: true, payable: null })}>
      <PlusCircle aria-hidden="true" />
      Nova despesa
    </Button>
  ) : null

  return (
    <div className="flex flex-col gap-6">
      <PageHeader title="Despesas" description={single ? 'Lançamentos operacionais com autorização do Master' : 'Lançamentos operacionais com dupla aprovação'} actions={actions} breadcrumbs={[{ label: 'Financeiro', to: '/financeiro' }, { label: 'Despesas' }]} />
      <LiveStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />
      {!master ? (
        <p className="m-0 text-sm text-text-muted">
          {single
            ? 'Somente os Administradores Master solicitam despesas; o lançamento exige a autorização do Master.'
            : 'Somente os Admin Master solicitam despesas; o lançamento exige a aprovação do outro sócio.'}
        </p>
      ) : null}
      {pendingExpenses.length ? (
        <Card>
          <CardHeader>
            <div className="flex flex-col gap-1">
              <CardTitle>{single ? 'Aguardando autorização' : 'Aguardando aprovação'}</CardTitle>
              <CardDescription>Ainda não lançadas no ledger.</CardDescription>
            </div>
            {master ? (
              <Link to="/financeiro/aprovacoes" className="text-sm font-semibold text-link underline-offset-4 hover:underline">
                {single ? 'Ir para autorizações' : 'Ir para aprovações'}
              </Link>
            ) : null}
          </CardHeader>
          <ul className="m-0 flex list-none flex-col divide-y divide-border p-0">
            {pendingExpenses.map((r) => (
              <li key={r.id} className="flex flex-wrap items-center justify-between gap-2 py-3 first:pt-0 last:pb-0">
                <div className="flex flex-col">
                  <span className="font-semibold">{String(r.payload.description ?? r.action_label)}</span>
                  <span className="text-xs text-text-muted">
                    {r.action_type === 'EXPENSE_UPDATE' ? 'Alteração' : 'Nova despesa'} · solicitada por {personLabel(r.requested_by_name, r.requested_by)} em {formatDateTime(r.requested_at)}
                  </span>
                </div>
                <div className="flex items-center gap-3">
                  <span className="font-semibold tabular-nums">{r.amount_cents !== null ? formatCents(r.amount_cents) : EMPTY_VALUE}</span>
                  <StatusPill map={APPROVAL_STATUS} value={r.status} />
                </div>
              </li>
            ))}
          </ul>
        </Card>
      ) : null}
      <Card>
        <CardHeader>
          <CardTitle>Despesas lançadas</CardTitle>
        </CardHeader>
        <DataTable
          caption="Despesas lançadas"
          columns={columns}
          data={q.data}
          getRowId={(p) => p.id}
          controller={table}
          isLoading={q.isLoading}
          isFetching={q.isFetching}
          error={q.error}
          onRetry={() => void q.refetch()}
          empty={{
            title: 'Nenhuma despesa lançada.',
            description: master ? 'Use “Nova despesa” para solicitar o primeiro lançamento.' : undefined,
            icon: Receipt,
          }}
          rowActions={
            master
              ? (p) =>
                  p.status === 'OPEN' ? (
                    <Button variant="ghost" size="icon" aria-label={`Alterar despesa ${p.description}`} onClick={() => setDialog({ open: true, payable: p })}>
                      <Pencil aria-hidden="true" />
                    </Button>
                  ) : null
              : undefined
          }
        />
      </Card>
      <ExpenseDialog open={dialog.open} payable={dialog.payable} onOpenChange={(o) => setDialog((d) => ({ ...d, open: o }))} />
    </div>
  )
}
