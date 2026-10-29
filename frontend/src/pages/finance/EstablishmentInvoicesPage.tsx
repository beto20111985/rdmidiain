/**
 * Minha unidade → Financeiro → Faturas: cobranças da RD ao parceiro (mensalidade, caução,
 * instalação, upgrade) — W-FIN `financial/establishments/{id}/charges`. PIX pendente com QR
 * gerado localmente e comprovante em PDF das pagas. Tempo real: `payment.updated` invalida 'invoices'.
 */
import * as React from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Download, Receipt, Store } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Sheet, SheetBody, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { EmptyState, ErrorState, PageSkeleton } from '@/components/patterns/states'
import { StatusPill } from '@/components/finance/status-pill'
import { DetailList } from '@/components/finance/detail-list'
import { PAYMENT_METHOD, PAYMENT_STATUS } from '@/components/finance/labels'
import { useContextOrgId, useSelectedEstablishment } from '@/components/finance/hooks'
import { EstablishmentPicker } from '@/components/finance/establishment-picker'
import { PixPayment } from '@/components/finance/pix-payment'
import { useDownload } from '@/components/finance/use-download'
import { LiveStale } from '@/components/finance/live-stale'
import { useTableState } from '@/hooks/useTableState'
import { http, type Paginated } from '@/lib/http'
import { qk } from '@/lib/queryKeys'
import { DEFAULT_TIMEZONE, EMPTY_VALUE, formatCents, formatDateTime } from '@/lib/format'
import type { PaymentOrder } from './api'

const PENDING = ['CREATED', 'PENDING', 'CREATION_UNKNOWN']
const PAID = ['PAID', 'PARTIALLY_REFUNDED', 'REFUNDED']

const CHARGE_KIND: Record<string, string> = {
  MONTHLY_FEE: 'Mensalidade',
  DEPOSIT: 'Caução (reembolsável)',
  INSTALLATION_FEE: 'Taxa de instalação',
  UPGRADE_FEE: 'Upgrade',
}

function chargeInfo(o: PaymentOrder): { kind: string; description: string } {
  const item = (o.items ?? [])[0] as { service_kind?: string; description?: string } | undefined
  return { kind: item?.service_kind ? (CHARGE_KIND[item.service_kind] ?? item.service_kind) : EMPTY_VALUE, description: item?.description ?? EMPTY_VALUE }
}

export default function EstablishmentInvoicesPage() {
  const orgId = useContextOrgId()
  const dl = useDownload()
  const { query: estQ, items, selected, select } = useSelectedEstablishment(orgId)
  const estId = selected?.id ?? null
  const tz = selected?.timezone || DEFAULT_TIMEZONE
  const table = useTableState({ defaultPageSize: 25 })
  const [open, setOpen] = React.useState<PaymentOrder | null>(null)
  const params = { page: table.state.page, page_size: table.state.pageSize }
  const q = useQuery({
    queryKey: qk.sub('invoices', 'partner-charges', estId ?? '', params),
    queryFn: () => http.get<Paginated<PaymentOrder>>(`/financial/establishments/${estId}/charges`, { query: params }),
    enabled: !!estId,
    placeholderData: keepPreviousData,
  })

  if (!orgId) {
    return (
      <>
        <PageHeader title="Faturas" />
        <EmptyState icon={Store} title="Selecione a sua unidade" description="Use o seletor de contexto para entrar em “Minha unidade”." />
      </>
    )
  }
  if (estQ.isLoading) return <PageSkeleton label="Carregando faturas…" />
  if (estQ.error) return <ErrorState error={estQ.error} onRetry={() => void estQ.refetch()} />
  if (!estId) {
    return (
      <>
        <PageHeader title="Faturas" />
        <EmptyState icon={Store} title="Nenhum estabelecimento vinculado" />
      </>
    )
  }

  const columns: DataTableColumn<PaymentOrder>[] = [
    { id: 'created', header: 'Emitida em', mobile: 'secondary', cell: (o) => formatDateTime(o.created_at, tz) },
    {
      id: 'kind',
      header: 'Cobrança',
      mobile: 'primary',
      cell: (o) => {
        const c = chargeInfo(o)
        return (
          <div className="flex flex-col">
            <span className="font-semibold">{c.kind}</span>
            <span className="text-xs text-text-muted">{c.description}</span>
          </div>
        )
      },
    },
    { id: 'amount', header: 'Valor', numeric: true, mobile: 'field', cell: (o) => formatCents(o.amount_cents) },
    { id: 'due', header: 'Vence em', mobile: 'field', cell: (o) => (PENDING.includes(o.status) ? formatDateTime(o.expires_at, tz) : EMPTY_VALUE) },
    { id: 'paid', header: 'Paga em', mobile: 'field', cell: (o) => formatDateTime(o.paid_at, tz) },
    { id: 'status', header: 'Situação', mobile: 'badge', cell: (o) => <StatusPill map={PAYMENT_STATUS} value={o.status} /> },
  ]
  const receipt = (o: PaymentOrder) =>
    void dl.run(o.id, `/financial/establishments/${estId}/charges/${o.id}/receipt`, { fallbackFilename: `comprovante-${o.id}.pdf` })

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Faturas"
        description="Cobranças da RD à unidade: mensalidade, caução, instalação e upgrade"
        breadcrumbs={[{ label: 'Minha unidade', to: '/unidade' }, { label: 'Financeiro' }, { label: 'Faturas' }]}
      />
      <EstablishmentPicker items={items} selectedId={estId} onSelect={select} />
      <LiveStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />
      <Card>
        <DataTable
          caption="Faturas da unidade"
          columns={columns}
          data={q.data}
          getRowId={(o) => o.id}
          controller={table}
          isLoading={q.isLoading}
          isFetching={q.isFetching}
          error={q.error}
          onRetry={() => void q.refetch()}
          empty={{ title: 'Nenhuma fatura emitida para a sua unidade.', description: 'Mensalidades e taxas do plano de parceiro aparecem aqui quando cobradas.', icon: Receipt }}
          rowActions={(o) =>
            PAID.includes(o.status) ? (
              <Button variant="secondary" loading={dl.isPending(o.id)} loadingText="Baixando…" onClick={() => receipt(o)} aria-label={`Baixar comprovante de ${formatCents(o.amount_cents)}`}>
                <Download aria-hidden="true" />
                Comprovante
              </Button>
            ) : (
              <Button variant="secondary" onClick={() => setOpen(o)} aria-label={`${PENDING.includes(o.status) ? 'Pagar fatura' : 'Detalhes da fatura'} de ${formatCents(o.amount_cents)}`}>
                {PENDING.includes(o.status) ? 'Pagar' : 'Detalhes'}
              </Button>
            )
          }
        />
      </Card>
      <Sheet open={!!open} onOpenChange={(v) => !v && setOpen(null)}>
        <SheetContent>
          <SheetHeader>
            <SheetTitle>Fatura</SheetTitle>
            <SheetDescription>{open ? chargeInfo(open).kind : ''}</SheetDescription>
          </SheetHeader>
          <SheetBody className="flex flex-col gap-5 pb-6">
            {open ? (
              <>
                <StatusPill map={PAYMENT_STATUS} value={open.status} className="self-start" />
                {open.provider_mode_fake ? <p className="m-0 text-xs font-semibold text-warning">Ambiente de testes: cobrança simulada, sem dinheiro real.</p> : null}
                {PENDING.includes(open.status) && open.pix_qr_code ? (
                  <PixPayment code={open.pix_qr_code} amountCents={open.amount_cents} expiresAt={open.expires_at} />
                ) : PENDING.includes(open.status) ? (
                  <Alert variant="info">
                    <AlertDescription>QR ainda indisponível para esta cobrança. Fale com o suporte se o problema persistir.</AlertDescription>
                  </Alert>
                ) : null}
                <DetailList
                  columns={1}
                  items={[
                    { label: 'Descrição', value: chargeInfo(open).description },
                    { label: 'Valor', value: formatCents(open.amount_cents), numeric: true },
                    { label: 'Forma de pagamento', value: open.method ? (PAYMENT_METHOD[open.method] ?? open.method) : EMPTY_VALUE },
                    { label: 'Emitida em', value: formatDateTime(open.created_at, tz) },
                    { label: 'Identificador', value: <span className="font-mono text-xs">{open.id}</span> },
                  ]}
                />
              </>
            ) : null}
          </SheetBody>
        </SheetContent>
      </Sheet>
    </div>
  )
}
