/** Diálogos de ação do detalhe da campanha: cancelar (com cotação de reembolso) e renovar. */
import * as React from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { ReasonDialog } from '@/components/campaigns/reason-dialog'
import { qk } from '@/lib/queryKeys'
import { newRequestId } from '@/lib/http'
import { isApiError } from '@/lib/errors'
import { formatCents, formatDateTime } from '@/lib/format'
import { campaignsApi } from '../lib/api'
import { PERIOD_LABEL } from '../lib/labels'
import { CheckoutError } from '../checkout/shared'
import type { CampaignDetail, CancelQuote, RenewalProposal } from '../lib/types'
import { describeError, isPaymentFlowError } from '@/pages/campaigns/lib/errorText'

const POLICY_TEXT: Record<string, string> = {
  FULL_BEFORE_START: 'Reembolso integral (antes do início da veiculação).',
  PROPORTIONAL: 'Reembolso proporcional ao período não veiculado.',
  WINDOW_EXPIRED: 'Fora da janela de reembolso: não há valor a devolver.',
  MIXED: 'Política mista por campanha.',
  ADMIN_OVERRIDE: 'Valor definido pelo financeiro.',
}

/** Cotação do reembolso vinda do servidor (W-CAMP cancel-quote → W-FIN quote_refund por cobrança). */
function useCancelQuote(campaign: CampaignDetail, enabled: boolean) {
  return useQuery<CancelQuote>({
    queryKey: qk.sub('campaigns', 'detail', campaign.id, 'cancel-quote'),
    enabled: enabled && !!campaign.paid_at,
    retry: false,
    queryFn: () => campaignsApi.cancelQuote(campaign.id),
  })
}

export function CancelCampaignDialog({
  campaign,
  open,
  onOpenChange,
  onDone,
}: {
  campaign: CampaignDetail
  open: boolean
  onOpenChange: (o: boolean) => void
  onDone: (c: CampaignDetail) => void
}) {
  const quote = useCancelQuote(campaign, open)
  const paid = !!campaign.paid_at
  const payments = quote.data?.payments ?? []
  const failed = payments.filter((p) => p.error)
  return (
    <ReasonDialog
      open={open}
      onOpenChange={onOpenChange}
      destructive
      title={`Cancelar a campanha ${campaign.name}?`}
      description="A campanha deixa de ser exibida e a reserva de telas é liberada. Esta ação não pode ser desfeita."
      reasonLabel="Motivo do cancelamento"
      minLength={10}
      requireText={campaign.name}
      confirmLabel="Cancelar campanha"
      onConfirm={async (reason) => onDone(await campaignsApi.cancel(campaign.id, reason))}
    >
      {paid ? (
        quote.isLoading ? (
          <Skeleton className="h-16 w-full" />
        ) : quote.data ? (
          <Alert variant={quote.data.refund_total_cents > 0 ? 'info' : 'warning'} data-testid="refund-quote">
            <div>
              <AlertTitle>Reembolso previsto: {formatCents(quote.data.refund_total_cents)}</AlertTitle>
              <AlertDescription>
                <ul className="m-0 flex list-none flex-col gap-1 p-0">
                  {payments
                    .filter((p) => !p.error)
                    .map((p) => (
                      <li key={p.payment_order_id}>
                        {POLICY_TEXT[p.policy ?? ''] ?? p.policy ?? ''}
                        {p.services_refund_cents ? ` Mídia ${formatCents(p.media_refund_cents ?? 0)} + serviços ${formatCents(p.services_refund_cents)}.` : ''}
                        {p.window_days ? ` Janela de ${p.window_days} dias.` : ''}
                      </li>
                    ))}
                </ul>
                {failed.length ? <p className="m-0">{failed.length} cobrança(s) sem cotação automática — o financeiro analisa manualmente.</p> : null}
                <p className="m-0">Cálculo do servidor; o reembolso passa por aprovação financeira.</p>
              </AlertDescription>
            </div>
          </Alert>
        ) : (
          <Alert variant="warning">
            <AlertDescription>
              Cotação de reembolso indisponível no momento ({quote.error ? describeError(quote.error) : 'cobrança não informada pelo servidor'}). O valor é calculado pelo
              financeiro após o cancelamento.
            </AlertDescription>
          </Alert>
        )
      ) : (
        <p className="m-0 text-sm text-text-muted">Campanha não paga: nenhum valor a reembolsar; cobranças pendentes são canceladas.</p>
      )}
    </ReasonDialog>
  )
}

/**
 * Proposta de renovação: cotação do período adicional pelo servidor. NÃO reserva telas nem gera
 * cobrança — a reserva de 2 h começa só no checkout da renovação (RenewalCard → "Gerar cobrança").
 */
export function RenewDialog({
  campaign,
  open,
  onOpenChange,
  onDone,
}: {
  campaign: CampaignDetail
  open: boolean
  onOpenChange: (o: boolean) => void
  onDone: (r: RenewalProposal) => void
}) {
  const [periods, setPeriods] = React.useState(1)
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<unknown>(null)
  React.useEffect(() => {
    if (open) {
      setPeriods(1)
      setError(null)
    }
  }, [open])

  const valid = periods >= 1 && periods <= 12
  const submit = async () => {
    if (!valid) return
    setPending(true)
    setError(null)
    try {
      onDone(await campaignsApi.renew(campaign.id, periods))
      onOpenChange(false)
    } catch (e) {
      setError(e)
    } finally {
      setPending(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => (!pending ? onOpenChange(o) : undefined)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Renovar campanha</DialogTitle>
          <DialogDescription>
            Estende a data final por mais períodos ({PERIOD_LABEL[campaign.period_type].toLowerCase()}), com as mesmas telas, anúncios e programação, sem nova
            análise. Primeiro o servidor calcula a proposta; as telas só ficam reservadas (por 2 horas) quando você gerar a cobrança.
          </DialogDescription>
        </DialogHeader>
        <Field invalid={!valid}>
          <FieldLabel>Quantidade de períodos</FieldLabel>
          <Input type="number" min={1} max={12} value={periods} onChange={(e) => setPeriods(Math.floor(Number(e.target.value) || 0))} />
          <FieldDescription>De 1 a 12.</FieldDescription>
        </Field>
        {error ? (
          isPaymentFlowError(error) ? (
            <CheckoutError error={error} />
          ) : (
            <Alert variant="danger">
              <AlertDescription>{describeError(error)}</AlertDescription>
            </Alert>
          )
        ) : null}
        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)} disabled={pending}>
            Cancelar
          </Button>
          <Button onClick={() => void submit()} loading={pending} disabled={!valid}>
            Calcular proposta de renovação
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

const STAGE_TEXT: Record<string, string> = {
  PROPOSTA: 'Proposta — sem reserva de telas e sem cobrança ainda',
  AGUARDANDO_PAGAMENTO: 'Aguardando pagamento — telas reservadas até o vencimento da cobrança',
  APLICADA: 'Renovação aplicada',
}

/**
 * Renovação aberta (manual ou automática D-5): proposta do servidor e, para o anunciante, o
 * checkout da renovação (Idempotency-Key por tentativa; repetida só em falha de rede). A contagem
 * de 2 h aparece somente na página de pagamento, a partir do `expires_at` do servidor.
 */
export function RenewalCard({
  campaignId,
  canPay,
  checkoutPath,
  onCheckout,
}: {
  campaignId: string
  canPay: boolean
  checkoutPath: (orderId: string) => string
  onCheckout: (orderId: string) => void
}) {
  const q = useQuery({
    queryKey: qk.sub('campaigns', 'detail', campaignId, 'renewal'),
    queryFn: async () => {
      try {
        return await campaignsApi.renewal(campaignId)
      } catch (e) {
        if (isApiError(e) && e.isNotFound) return null // sem renovação aberta
        throw e
      }
    },
    retry: false,
  })
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<unknown>(null)
  const keyRef = React.useRef<string | null>(null)
  const r = q.data
  if (q.isLoading || !r) return null

  const pay = async () => {
    setPending(true)
    setError(null)
    if (!keyRef.current) keyRef.current = newRequestId()
    try {
      const res = await campaignsApi.renewCheckout(campaignId, keyRef.current)
      keyRef.current = null
      onCheckout(res.order_id)
    } catch (e) {
      if (!(isApiError(e) && e.isNetwork)) keyRef.current = null
      setError(e)
    } finally {
      setPending(false)
    }
  }

  return (
    <section className="flex flex-col gap-3 rounded-lg border border-border p-4" aria-labelledby="renewal-title" data-testid="renewal-card">
      <h3 id="renewal-title" className="text-title-card">
        Renovação {r.auto ? 'automática' : 'solicitada'} · {STAGE_TEXT[r.stage] ?? r.stage}
      </h3>
      <dl className="m-0 grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
        <div>
          <dt className="text-xs text-text-muted">Períodos</dt>
          <dd className="m-0">{r.periods ?? '—'}</dd>
        </div>
        <div>
          <dt className="text-xs text-text-muted">Novo fim</dt>
          <dd className="m-0">{r.new_end_date ? r.new_end_date.split('-').reverse().join('/') : '—'}</dd>
        </div>
        <div>
          <dt className="text-xs text-text-muted">Valor (servidor)</dt>
          <dd className="m-0 font-semibold tabular-nums" data-testid="renewal-total">
            {formatCents(r.total_cents)}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-text-muted">Pagar até</dt>
          <dd className="m-0">{formatDateTime(r.deadline)}</dd>
        </div>
      </dl>
      {r.stage === 'PROPOSTA' ? (
        <p className="m-0 text-sm text-text-muted">
          As telas ainda não estão reservadas. Ao gerar a cobrança, elas ficam reservadas por 2 horas (ou até o fim do período atual, o que vier primeiro). Sem
          pagamento até o prazo, a campanha termina às 23:59:59 do último dia.
        </p>
      ) : null}
      {error ? (
        isPaymentFlowError(error) ? (
          <CheckoutError error={error} />
        ) : (
          <Alert variant="danger">
            <AlertDescription>{describeError(error)}</AlertDescription>
          </Alert>
        )
      ) : null}
      {canPay ? (
        r.stage === 'PROPOSTA' ? (
          <Button className="w-fit" onClick={() => void pay()} loading={pending}>
            Gerar cobrança PIX da renovação
          </Button>
        ) : r.stage === 'AGUARDANDO_PAGAMENTO' && r.order?.order_id ? (
          <Button className="w-fit" asChild>
            <Link to={checkoutPath(r.order.order_id)}>Ver pagamento da renovação</Link>
          </Button>
        ) : null
      ) : null}
    </section>
  )
}
