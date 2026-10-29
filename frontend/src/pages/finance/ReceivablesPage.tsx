/**
 * Financeiro → Contas a receber (ETAPA2 #652–#660): cobranças CloudPayPag (pendentes por
 * padrão, ou por situação), total pendente, detalhe com IDs interno/externo, e reembolsos.
 * Tempo real: `payment.updated` invalida 'finance'.
 */
import * as React from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { ArrowDownToLine } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Field, FieldLabel } from '@/components/ui/field'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { KpiCard } from '@/components/patterns/kpi-card'
import { StatusPill } from '@/components/finance/status-pill'
import { PAYMENT_METHOD, PAYMENT_STATUS, REFUND_STATUS } from '@/components/finance/labels'
import { useOrgNames } from '@/components/finance/hooks'
import { LiveStale } from '@/components/finance/live-stale'
import { useTableState } from '@/hooks/useTableState'
import { qk } from '@/lib/queryKeys'
import { EMPTY_VALUE, formatCents, formatDateTime } from '@/lib/format'
import { useInstanceApprovalMode } from '@/components/finance/approval-utils'
import { financeApi, type PaymentOrder, type Refund } from './api'
import { PaymentOrderSheet } from './payment-order-sheet'

const STATUS_OPTIONS = ['PENDING_LIKE', 'PAID', 'PARTIALLY_REFUNDED', 'REFUNDED', 'EXPIRED', 'CANCELLED', 'FAILED', 'CREATION_FAILED', 'CREATION_UNKNOWN']

export default function ReceivablesPage() {
  const single = useInstanceApprovalMode().isSingle
  const orgs = useOrgNames()
  const table = useTableState({ defaultPageSize: 25, filterKeys: ['status'] })
  const refundsTable = useTableState({ defaultPageSize: 10, prefix: 'r_' })
  const status = table.state.filters.status
  const [open, setOpen] = React.useState<PaymentOrder | null>(null)

  const params = { page: table.state.page, page_size: table.state.pageSize, status: status && status !== 'PENDING_LIKE' ? status : undefined }
  const q = useQuery({
    queryKey: qk.sub('finance', 'receivables', params),
    queryFn: () => financeApi.receivables(params),
    placeholderData: keepPreviousData,
  })
  const rParams = { page: refundsTable.state.page, page_size: refundsTable.state.pageSize }
  const refunds = useQuery({
    queryKey: qk.sub('finance', 'refunds', rParams),
    queryFn: () => financeApi.refunds(rParams),
    placeholderData: keepPreviousData,
  })

  const columns: DataTableColumn<PaymentOrder>[] = [
    { id: 'created', header: 'Criada em', mobile: 'secondary', cell: (o) => formatDateTime(o.created_at) },
    { id: 'org', header: 'Cliente', mobile: 'primary', cell: (o) => <span className="font-semibold">{orgs.nameOf(o.org_id)}</span> },
    { id: 'amount', header: 'Valor', numeric: true, mobile: 'field', cell: (o) => formatCents(o.amount_cents) },
    { id: 'method', header: 'Forma', mobile: 'field', cell: (o) => (o.method ? (PAYMENT_METHOD[o.method] ?? o.method) : EMPTY_VALUE) },
    { id: 'due', header: 'Vence em', mobile: 'field', cell: (o) => formatDateTime(o.expires_at) },
    { id: 'paid', header: 'Paga em', mobile: 'field', cell: (o) => formatDateTime(o.paid_at) },
    { id: 'net', header: 'Líquido', numeric: true, mobile: 'field', cell: (o) => formatCents(o.net_cents) },
    { id: 'status', header: 'Situação', mobile: 'badge', cell: (o) => <StatusPill map={PAYMENT_STATUS} value={o.status} /> },
  ]
  const refundCols: DataTableColumn<Refund>[] = [
    { id: 'created', header: 'Solicitado em', mobile: 'primary', cell: (r) => formatDateTime(r.created_at) },
    { id: 'amount', header: 'Valor', numeric: true, mobile: 'field', cell: (r) => formatCents(r.refund_cents) },
    { id: 'policy', header: 'Política', mobile: 'field', cell: (r) => ({ FULL_BEFORE_START: 'Integral (antes do início)', PROPORTIONAL: 'Proporcional', ADMIN_OVERRIDE: 'Definido pela RD' })[r.policy] ?? r.policy },
    { id: 'reason', header: 'Motivo', mobile: 'field', cell: (r) => <span className="line-clamp-2">{r.reason}</span> },
    { id: 'status', header: 'Situação', mobile: 'badge', cell: (r) => <StatusPill map={REFUND_STATUS} value={r.status} /> },
  ]

  return (
    <div className="flex flex-col gap-6">
      <PageHeader title="Contas a receber" description="Cobranças de anunciantes (CloudPayPag)" breadcrumbs={[{ label: 'Financeiro', to: '/financeiro' }, { label: 'Contas a receber' }]} />
      <LiveStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4 xl:gap-6">
        <KpiCard label="Total pendente" value={q.data?.pending_total_cents} format={formatCents} loading={q.isLoading} description="Cobranças aguardando pagamento." />
      </div>
      <Card>
        <DataTable
          caption="Cobranças"
          columns={columns}
          data={q.data}
          getRowId={(o) => o.id}
          controller={table}
          isLoading={q.isLoading}
          isFetching={q.isFetching}
          error={q.error}
          onRetry={() => void q.refetch()}
          filtersActive={!!status}
          empty={{ title: 'Nenhuma cobrança pendente.', description: 'Cobranças são criadas no checkout das campanhas e na conversão de propostas.', icon: ArrowDownToLine }}
          toolbar={
            <Field className="w-full max-w-xs">
              <FieldLabel>Situação</FieldLabel>
              <Select value={status ?? 'PENDING_LIKE'} onValueChange={(v) => table.setFilter('status', v === 'PENDING_LIKE' ? null : v)}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    {STATUS_OPTIONS.map((s) => (
                      <SelectItem key={s} value={s}>
                        {s === 'PENDING_LIKE' ? 'Pendentes (padrão)' : PAYMENT_STATUS[s]?.label ?? s}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
            </Field>
          }
          rowActions={(o) => (
            <Button variant="secondary" onClick={() => setOpen(o)} aria-label={`Ver cobrança de ${formatCents(o.amount_cents)}`}>
              Detalhes
            </Button>
          )}
        />
      </Card>
      <Card>
        <CardHeader>
          <div className="flex flex-col gap-1">
            <CardTitle>Reembolsos</CardTitle>
            <CardDescription>
              {single ? 'Todo reembolso exige autorização do Master' : 'Todo reembolso exige dupla aprovação'}; sem API de estorno, o pagamento é por PIX OUT ou fica bloqueado por dependência externa.
            </CardDescription>
          </div>
        </CardHeader>
        <DataTable
          caption="Reembolsos"
          columns={refundCols}
          data={refunds.data}
          getRowId={(r) => r.id}
          controller={refundsTable}
          isLoading={refunds.isLoading}
          isFetching={refunds.isFetching}
          error={refunds.error}
          onRetry={() => void refunds.refetch()}
          empty={{ title: 'Nenhum reembolso registrado.' }}
        />
      </Card>
      <PaymentOrderSheet order={open} onOpenChange={(o) => !o && setOpen(null)} orgName={orgs.nameOf} />
    </div>
  )
}
