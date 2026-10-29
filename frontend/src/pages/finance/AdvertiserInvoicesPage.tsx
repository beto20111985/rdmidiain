/**
 * Minhas campanhas → Faturas (ETAPA2 #753; PG-18): faturas/pedidos e situação, PIX pendente
 * com QR gerado LOCALMENTE + copia-e-cola, comprovante de pagamento (PDF) e solicitação de
 * nota fiscal (porta pronta; fornecedor = dependência externa). Tempo real: `payment.updated`
 * invalida 'invoices'.
 */
import * as React from 'react'
import { Link } from 'react-router-dom'
import { keepPreviousData, useMutation, useQuery } from '@tanstack/react-query'
import { Download, FileText, Receipt } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Field, FieldLabel } from '@/components/ui/field'
import { Sheet, SheetBody, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { EmptyState, ErrorState } from '@/components/patterns/states'
import { StatusPill } from '@/components/finance/status-pill'
import { DetailList } from '@/components/finance/detail-list'
import { PAYMENT_METHOD, PAYMENT_STATUS, REFUND_STATUS } from '@/components/finance/labels'
import { useContextOrgId } from '@/components/finance/hooks'
import { useDownload } from '@/components/finance/use-download'
import { LiveStale } from '@/components/finance/live-stale'
import { PixPayment } from '@/components/finance/pix-payment'
import { useTableState } from '@/hooks/useTableState'
import { qk } from '@/lib/queryKeys'
import { errorMessage } from '@/lib/errors'
import { EMPTY_VALUE, formatCents, formatDateTime } from '@/lib/format'
import { financeApi, type PaymentOrder, type Refund } from './api'

const PENDING = ['CREATED', 'PENDING', 'CREATION_UNKNOWN']
const PAID = ['PAID', 'PARTIALLY_REFUNDED', 'REFUNDED']

function InvoiceSheet({ id, onOpenChange }: { id: string | null; onOpenChange: (o: boolean) => void }) {
  const dl = useDownload()
  const q = useQuery({ queryKey: qk.sub('invoices', 'detail', id ?? ''), queryFn: () => financeApi.invoice(id as string), enabled: !!id })
  const fiscal = useMutation({ mutationFn: () => financeApi.requestFiscalDocument(id as string) })
  const o = q.data
  return (
    <Sheet open={!!id} onOpenChange={onOpenChange}>
      <SheetContent>
        <SheetHeader>
          <SheetTitle>Fatura</SheetTitle>
          <SheetDescription>Situação, pagamento e comprovante.</SheetDescription>
        </SheetHeader>
        <SheetBody className="flex flex-col gap-5 pb-6">
          {q.isLoading ? (
            <Skeleton className="h-48 w-full" />
          ) : q.error ? (
            <ErrorState error={q.error} onRetry={() => void q.refetch()} size="compact" />
          ) : o ? (
            <>
              <StatusPill map={PAYMENT_STATUS} value={o.status} className="self-start" />
              {o.provider_mode_fake ? <p className="m-0 text-xs font-semibold text-warning">Ambiente de testes: cobrança simulada, sem dinheiro real.</p> : null}
              {PENDING.includes(o.status) && o.pix_qr_code ? <PixPayment code={o.pix_qr_code} amountCents={o.amount_cents} expiresAt={o.expires_at} /> : null}
              {PENDING.includes(o.status) && !o.pix_qr_code ? (
                <Alert variant="info">
                  <AlertDescription>Cobrança sem QR disponível ({PAYMENT_METHOD[o.method ?? ''] ?? o.method ?? 'forma não informada'}).</AlertDescription>
                </Alert>
              ) : null}
              <DetailList
                columns={1}
                items={[
                  { label: 'Valor', value: formatCents(o.amount_cents), numeric: true },
                  { label: 'Forma de pagamento', value: o.method ? (PAYMENT_METHOD[o.method] ?? o.method) : EMPTY_VALUE },
                  { label: 'Criada em', value: formatDateTime(o.created_at) },
                  { label: 'Paga em', value: formatDateTime(o.paid_at) },
                  { label: 'Reembolsado', value: formatCents(o.refunded_cents), numeric: true },
                  { label: 'Identificador', value: <span className="font-mono text-xs">{o.id}</span> },
                ]}
              />
              {PAID.includes(o.status) ? (
                <div className="flex flex-wrap gap-3">
                  <Button
                    variant="secondary"
                    loading={dl.isPending('receipt')}
                    loadingText="Baixando…"
                    onClick={() => void dl.run('receipt', `/financial/invoices/${o.id}/receipt`, { fallbackFilename: `comprovante-${o.id}.pdf` })}
                  >
                    <Download aria-hidden="true" />
                    Comprovante de pagamento (PDF)
                  </Button>
                  <Button variant="secondary" loading={fiscal.isPending} loadingText="Solicitando…" onClick={() => fiscal.mutate()}>
                    <FileText aria-hidden="true" />
                    Solicitar nota fiscal
                  </Button>
                </div>
              ) : null}
              {fiscal.data ? (
                <Alert variant={fiscal.data.status === 'BLOCKED_EXTERNAL_DEPENDENCY' ? 'warning' : 'info'}>
                  <div className="flex min-w-0 flex-1 flex-col gap-1">
                    <AlertTitle>{fiscal.data.status === 'BLOCKED_EXTERNAL_DEPENDENCY' ? 'Nota fiscal: BLOQUEADO POR DEPENDÊNCIA EXTERNA' : `Nota fiscal: ${fiscal.data.status}`}</AlertTitle>
                    {fiscal.data.detail ? <AlertDescription>{fiscal.data.detail}</AlertDescription> : null}
                  </div>
                </Alert>
              ) : null}
              {fiscal.error ? (
                <Alert variant="danger">
                  <AlertDescription>{errorMessage(fiscal.error)}</AlertDescription>
                </Alert>
              ) : null}
            </>
          ) : null}
        </SheetBody>
      </SheetContent>
    </Sheet>
  )
}

export default function AdvertiserInvoicesPage() {
  const orgId = useContextOrgId()
  const table = useTableState({ defaultPageSize: 25, filterKeys: ['status'] })
  const refundsTable = useTableState({ defaultPageSize: 10, prefix: 'r_' })
  const [openId, setOpenId] = React.useState<string | null>(null)
  const params = { org_id: orgId ?? undefined, page: table.state.page, page_size: table.state.pageSize, status: table.state.filters.status }
  const q = useQuery({ queryKey: qk.list('invoices', params), queryFn: () => financeApi.invoices(params), enabled: !!orgId, placeholderData: keepPreviousData })
  const rParams = { page: refundsTable.state.page, page_size: refundsTable.state.pageSize }
  const refunds = useQuery({ queryKey: qk.sub('invoices', 'refunds', orgId ?? '', rParams), queryFn: () => financeApi.refunds(rParams), enabled: !!orgId, placeholderData: keepPreviousData })

  if (!orgId) {
    return (
      <>
        <PageHeader title="Faturas" />
        <EmptyState icon={Receipt} title="Selecione a empresa anunciante" description="Use o seletor de contexto para entrar em “Minhas campanhas”." />
      </>
    )
  }

  const columns: DataTableColumn<PaymentOrder>[] = [
    { id: 'created', header: 'Data', mobile: 'secondary', cell: (o) => formatDateTime(o.created_at) },
    { id: 'amount', header: 'Valor', numeric: true, mobile: 'primary', cell: (o) => <span className="font-semibold">{formatCents(o.amount_cents)}</span> },
    { id: 'method', header: 'Forma', mobile: 'field', cell: (o) => (o.method ? (PAYMENT_METHOD[o.method] ?? o.method) : EMPTY_VALUE) },
    { id: 'due', header: 'Vence em', mobile: 'field', cell: (o) => (PENDING.includes(o.status) ? formatDateTime(o.expires_at) : EMPTY_VALUE) },
    { id: 'paid', header: 'Paga em', mobile: 'field', cell: (o) => formatDateTime(o.paid_at) },
    { id: 'status', header: 'Situação', mobile: 'badge', cell: (o) => <StatusPill map={PAYMENT_STATUS} value={o.status} /> },
  ]
  const refundCols: DataTableColumn<Refund>[] = [
    { id: 'date', header: 'Solicitado em', mobile: 'primary', cell: (r) => formatDateTime(r.created_at) },
    { id: 'amount', header: 'Valor', numeric: true, mobile: 'field', cell: (r) => formatCents(r.refund_cents) },
    { id: 'status', header: 'Situação', mobile: 'badge', cell: (r) => <StatusPill map={REFUND_STATUS} value={r.status} /> },
  ]

  return (
    <div className="flex flex-col gap-6">
      <PageHeader title="Faturas" description="Pedidos, pagamentos e comprovantes" />
      <LiveStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />
      <Card>
        <DataTable
          caption="Faturas"
          columns={columns}
          data={q.data}
          getRowId={(o) => o.id}
          controller={table}
          isLoading={q.isLoading}
          isFetching={q.isFetching}
          error={q.error}
          onRetry={() => void q.refetch()}
          filtersActive={!!table.state.filters.status}
          empty={{
            title: 'Nenhuma fatura ainda.',
            description: 'As faturas aparecem quando você contrata uma campanha.',
            icon: Receipt,
            action: (
              <Button asChild variant="secondary">
                <Link to="/anunciante/campanhas/nova">Criar campanha</Link>
              </Button>
            ),
          }}
          toolbar={
            <Field className="w-full max-w-xs">
              <FieldLabel>Situação</FieldLabel>
              <Select value={table.state.filters.status ?? 'all'} onValueChange={(v) => table.setFilter('status', v === 'all' ? null : v)}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    <SelectItem value="all">Todas</SelectItem>
                    {['PENDING', 'PAID', 'EXPIRED', 'CANCELLED', 'REFUNDED', 'PARTIALLY_REFUNDED'].map((s) => (
                      <SelectItem key={s} value={s}>
                        {PAYMENT_STATUS[s].label}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
            </Field>
          }
          rowActions={(o) => (
            <Button variant="secondary" onClick={() => setOpenId(o.id)} aria-label={`${PENDING.includes(o.status) ? 'Pagar fatura' : 'Detalhes da fatura'} de ${formatCents(o.amount_cents)}`}>
              {PENDING.includes(o.status) ? 'Pagar' : 'Detalhes'}
            </Button>
          )}
        />
      </Card>
      <Card>
        <CardHeader>
          <div className="flex flex-col gap-1">
            <CardTitle>Reembolsos</CardTitle>
            <CardDescription>Solicitações de reembolso das suas campanhas e a situação de cada uma.</CardDescription>
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
          empty={{ title: 'Nenhum reembolso.' }}
        />
      </Card>
      <p className="m-0 text-sm text-text-muted">
        Comprovantes de VEICULAÇÃO (exibições válidas) ficam em{' '}
        <Link to="/anunciante/comprovantes" className="font-semibold text-link underline-offset-4 hover:underline">
          Comprovantes
        </Link>
        .
      </p>
      <InvoiceSheet id={openId} onOpenChange={(o) => !o && setOpenId(null)} />
    </div>
  )
}
