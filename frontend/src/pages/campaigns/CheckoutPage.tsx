/**
 * Pagamento do pedido (PG-17): PIX com QR renderizado localmente + copia e cola, reserva de
 * 2 h com contagem regressiva pelo `expires_at` do servidor, atualização em tempo real do
 * pagamento (`payment.updated`) e do status das campanhas (`campaign.updated`) — sem polling.
 */
import * as React from 'react'
import { Link, useParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { CheckCircle2, CreditCard } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ConfirmDialog } from '@/components/patterns/confirm-dialog'
import { ErrorState, NoPermission, PageSkeleton, StaleData } from '@/components/patterns/states'
import { ToneBadge } from '@/components/campaigns/badges'
import { Countdown, PixPayment } from '@/components/campaigns/pix-payment'
import { useRealtimeEvent, useRealtimeStatus } from '@/lib/realtime/context'
import { qk } from '@/lib/queryKeys'
import { isApiError } from '@/lib/errors'
import { formatCents, formatDateTime } from '@/lib/format'
import { checkoutApi } from './lib/api'
import { ORDER_STATUS } from './lib/labels'
import { useCampaignArea } from './lib/hooks'
import { CampaignLabel } from './checkout/shared'

const KIND_LABEL: Record<string, string> = { CHECKOUT: 'Compra', CHANGE: 'Alteração de campanha', RENEWAL: 'Renovação', PROPOSAL: 'Proposta comercial' }
/** O que acontece depois do pagamento, por tipo de pedido (renovação não passa por nova análise). */
const PAID_NEXT: Record<string, string> = {
  CHECKOUT: 'As campanhas seguem para a análise da RD (ou ficam aguardando os anúncios). Acompanhe o status abaixo.',
  CHANGE: 'A alteração segue para a análise da RD; a versão atual continua no ar até a decisão.',
  RENEWAL: 'A renovação foi aplicada com as mesmas telas, anúncios e programação, sem nova análise.',
}

export default function CheckoutPage() {
  const { id = '' } = useParams()
  const area = useCampaignArea()
  const qc = useQueryClient()
  const { live } = useRealtimeStatus()
  const [cancelOpen, setCancelOpen] = React.useState(false)
  const q = useQuery({ queryKey: qk.detail('checkout', id), queryFn: () => checkoutApi.order(id), enabled: !!id })
  const order = q.data

  useRealtimeEvent('payment.updated', (ev) => {
    const d = ev.data as { id?: string; status?: string; reference_id?: string }
    if (!order) return
    if (d.reference_id === order.id || (order.payment_order_id && d.id === order.payment_order_id)) {
      if (d.status === 'PAID') toast.success('Pagamento confirmado.')
      void qc.invalidateQueries({ queryKey: qk.detail('checkout', id) })
    }
  })

  const breadcrumbs = React.useMemo(() => [{ label: 'Carrinho', to: area.cartPath }, { label: 'Pagamento' }], [area.cartPath])
  const onExpire = React.useCallback(() => void q.refetch(), [q])

  if (q.isLoading) return <PageSkeleton label="Carregando pagamento…" />
  if (q.error || !order) {
    if (isApiError(q.error) && q.error.isForbidden) return <NoPermission permission="checkout.create" />
    return <ErrorState error={q.error} title={isApiError(q.error) && q.error.isNotFound ? 'Pedido não encontrado ou fora do seu acesso' : undefined} onRetry={() => void q.refetch()} />
  }
  const code = order.payment?.pix_qr_code || order.payment?.pix_copy_paste || null
  const waiting = order.status === 'AGUARDANDO_PAGAMENTO'

  return (
    <>
      <PageHeader title="Pagamento" description={`${KIND_LABEL[order.kind] ?? order.kind} · pedido ${order.id.slice(0, 8)}`} breadcrumbs={breadcrumbs} />
      <div className="flex flex-col gap-6">
        {!live ? (
          <StaleData updatedAt={q.dataUpdatedAt} reason="tempo real indisponível: a confirmação do pagamento pode demorar a aparecer." onRefresh={() => void q.refetch()} refreshing={q.isFetching} />
        ) : null}
        <div className="grid grid-cols-1 gap-6 xl:grid-cols-12">
          <Card className="xl:col-span-7">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <CardTitle>Total a pagar</CardTitle>
              <ToneBadge {...ORDER_STATUS[order.status]} />
            </div>
            <p className="m-0 text-3xl font-bold tabular-nums" data-testid="order-total">
              {formatCents(order.total_cents)}
            </p>
            {waiting ? <Countdown expiresAt={order.expires_at} onExpire={onExpire} /> : null}

            {order.status === 'PAGO' ? (
              <Alert variant="success">
                <CheckCircle2 aria-hidden="true" />
                <div>
                  <AlertTitle>Pagamento confirmado em {formatDateTime(order.paid_at)}</AlertTitle>
                  <AlertDescription>{PAID_NEXT[order.kind] ?? PAID_NEXT.CHECKOUT}</AlertDescription>
                </div>
              </Alert>
            ) : null}
            {order.status === 'EXPIRADO' ? (
              <Alert variant="warning">
                <AlertTitle>Reserva expirada</AlertTitle>
                <AlertDescription>O prazo de 2 horas terminou e as telas foram liberadas. Gere uma nova cobrança no carrinho.</AlertDescription>
              </Alert>
            ) : null}
            {order.status === 'CANCELADO' ? (
              <Alert variant="neutral">
                <AlertDescription>Pedido cancelado. Nenhum valor será cobrado.</AlertDescription>
              </Alert>
            ) : null}

            {waiting ? (
              code ? (
                <PixPayment code={code} />
              ) : (
                <Alert variant="warning">
                  <AlertDescription>
                    A cobrança PIX ainda não foi confirmada pelo meio de pagamento (status {order.payment?.status ?? 'desconhecido'}). Assim que o código estiver
                    disponível, esta página é atualizada automaticamente.
                  </AlertDescription>
                </Alert>
              )
            ) : null}

            {waiting ? (
              <div className="flex flex-col gap-2 rounded-lg border border-border p-3" data-testid="card-unavailable">
                <span className="flex items-center gap-2 text-sm font-semibold">
                  <CreditCard aria-hidden="true" className="size-4" />
                  Cartão de crédito
                </span>
                <span className="text-sm text-text-muted">Pagamento por cartão indisponível neste ambiente.</span>
              </div>
            ) : null}

            <div className="flex flex-wrap gap-3 border-t border-border pt-4">
              <Button variant="secondary" asChild>
                <Link to={area.campaignsBase}>Minhas campanhas</Link>
              </Button>
              {waiting ? (
                <Button variant="destructive" onClick={() => setCancelOpen(true)}>
                  Cancelar pedido
                </Button>
              ) : null}
              {order.status === 'EXPIRADO' || order.status === 'CANCELADO' ? (
                <Button asChild>
                  <Link to={area.cartPath}>Voltar ao carrinho</Link>
                </Button>
              ) : null}
            </div>
          </Card>

          <Card className="xl:col-span-5">
            <CardTitle>Itens do pedido</CardTitle>
            <CardDescription>Valores congelados pelo servidor na geração da cobrança.</CardDescription>
            {order.items?.length ? (
              <Table>
                <caption className="sr-only">Itens do pedido</caption>
                <TableHeader>
                  <TableRow className="h-11 hover:bg-transparent">
                    <TableHead scope="col">Campanha</TableHead>
                    <TableHead scope="col" numeric>
                      Valor
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {order.items.map((it) => (
                    <TableRow key={it.campaign_id}>
                      <TableCell>
                        <CampaignLabel id={it.campaign_id} base={area.campaignsBase} />
                        {it.discount_cents ? <span className="block text-xs text-success">desconto {formatCents(-Math.abs(it.discount_cents))}</span> : null}
                      </TableCell>
                      <TableCell numeric className="tabular-nums">
                        {formatCents(it.amount_cents)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <p className="m-0 text-sm text-text-muted">—</p>
            )}
            <dl className="m-0 grid grid-cols-2 gap-2 text-sm">
              <dt className="text-text-muted">Mídia</dt>
              <dd className="m-0 text-right tabular-nums">{formatCents(order.media_cents)}</dd>
              <dt className="text-text-muted">Serviços</dt>
              <dd className="m-0 text-right tabular-nums">{formatCents(order.fees_cents)}</dd>
              <dt className="text-text-muted">Descontos</dt>
              <dd className="m-0 text-right tabular-nums">{order.discount_cents ? formatCents(-Math.abs(order.discount_cents)) : formatCents(0)}</dd>
              <dt className="font-semibold">Total</dt>
              <dd className="m-0 text-right font-semibold tabular-nums">{formatCents(order.total_cents)}</dd>
            </dl>
          </Card>
        </div>
      </div>
      <ConfirmDialog
        open={cancelOpen}
        onOpenChange={setCancelOpen}
        destructive
        title="Cancelar este pedido?"
        description="A reserva das telas é liberada e as campanhas voltam para rascunho. Não pague o PIX depois de cancelar."
        confirmLabel="Cancelar pedido"
        onConfirm={async () => {
          await checkoutApi.cancelOrder(order.id)
          toast.success('Pedido cancelado.')
          void qc.invalidateQueries({ queryKey: qk.root('checkout') })
          void qc.invalidateQueries({ queryKey: qk.root('campaigns') })
        }}
      />
    </>
  )
}
