/** Detalhe de uma cobrança (PaymentOrder) — IDs internos e do provedor, valores e PIX. */
import { Sheet, SheetBody, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { DetailList } from '@/components/finance/detail-list'
import { StatusPill } from '@/components/finance/status-pill'
import { PAYMENT_METHOD, PAYMENT_STATUS } from '@/components/finance/labels'
import { EMPTY_VALUE, formatCents, formatDateTime } from '@/lib/format'
import type { PaymentOrder } from './api'

export function PaymentOrderSheet({
  order,
  onOpenChange,
  orgName,
}: {
  order: PaymentOrder | null
  onOpenChange: (open: boolean) => void
  orgName?: (id: string) => string
}) {
  return (
    <Sheet open={!!order} onOpenChange={onOpenChange}>
      <SheetContent>
        <SheetHeader>
          <SheetTitle>Cobrança</SheetTitle>
          <SheetDescription>ID interno e identificadores da CloudPayPag para conciliação.</SheetDescription>
        </SheetHeader>
        <SheetBody className="flex flex-col gap-4 pb-6">
          {order ? (
            <>
              <StatusPill map={PAYMENT_STATUS} value={order.status} className="self-start" />
              {order.provider_mode_fake ? (
                <p className="m-0 text-xs font-semibold text-warning">Provedor simulado (desenvolvimento) — sem dinheiro real.</p>
              ) : null}
              <DetailList
                columns={1}
                items={[
                  ...(orgName ? [{ label: 'Organização', value: orgName(order.org_id) }] : []),
                  { label: 'Valor', value: formatCents(order.amount_cents), numeric: true },
                  { label: 'Receita elegível (mídia)', value: formatCents(order.eligible_revenue_cents), numeric: true },
                  { label: 'Serviços', value: formatCents(order.services_cents), numeric: true },
                  { label: 'Forma de pagamento', value: order.method ? (PAYMENT_METHOD[order.method] ?? order.method) : EMPTY_VALUE },
                  { label: 'Pago (valor)', value: formatCents(order.paid_amount_cents), numeric: true },
                  { label: 'Taxa do provedor', value: formatCents(order.fee_cents), numeric: true },
                  { label: 'Líquido', value: formatCents(order.net_cents), numeric: true },
                  { label: 'Reembolsado', value: formatCents(order.refunded_cents), numeric: true },
                  { label: 'Criada em', value: formatDateTime(order.created_at) },
                  { label: 'Vence em', value: formatDateTime(order.expires_at) },
                  { label: 'Paga em', value: formatDateTime(order.paid_at) },
                  { label: 'Contestação', value: order.dispute_status ?? EMPTY_VALUE },
                  { label: 'Referência', value: order.reference_type ? `${order.reference_type} · ${order.reference_id ?? ''}` : EMPTY_VALUE },
                  { label: 'ID interno', value: <span className="font-mono text-xs">{order.id}</span> },
                  { label: 'ID externo (provedor)', value: <span className="font-mono text-xs">{order.provider_external_ref ?? EMPTY_VALUE}</span> },
                  { label: 'Transação no provedor', value: <span className="font-mono text-xs">{order.provider_transaction_id ?? EMPTY_VALUE}</span> },
                  { label: 'PIX end-to-end', value: <span className="font-mono text-xs">{order.pix_end_to_end_id ?? EMPTY_VALUE}</span> },
                ]}
              />
            </>
          ) : null}
        </SheetBody>
      </SheetContent>
    </Sheet>
  )
}
