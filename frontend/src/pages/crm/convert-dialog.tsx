/**
 * Conversão proposta → pedido → checkout (REQ-CRM-005): usa o MESMO checkout do W-CAMP
 * (preço recalculado no servidor, reserva de inventário por 2 h, cobrança CloudPayPag).
 * Mostra o resultado real (pedido, total, cobrança, avisos) ou o erro do checkout.
 */
import * as React from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Field, FieldGroup, FieldLabel, FieldLegend, FieldSet } from '@/components/ui/field'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Skeleton } from '@/components/ui/skeleton'
import { DetailList } from '@/components/finance/detail-list'
import { StatusPill } from '@/components/finance/status-pill'
import { CAMPAIGN_STATUS, PAYMENT_STATUS } from '@/components/finance/labels'
import { formatIsoDate } from '@/components/finance/period'
import { newRequestId } from '@/lib/http'
import { qk } from '@/lib/queryKeys'
import { errorMessage, isApiError } from '@/lib/errors'
import { formatCents, formatDateTime } from '@/lib/format'
import { paymentErrorInfo } from '@/components/finance/payment-errors'
import { crmApi, type ConvertResult, type Proposal } from './api'

export function ConvertDialog({ proposal, open, onOpenChange }: { proposal: Proposal; open: boolean; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient()
  const [selected, setSelected] = React.useState<string[]>([])
  const [coupon, setCoupon] = React.useState('')
  const [fieldError, setFieldError] = React.useState<string | null>(null)
  const [error, setError] = React.useState<{ title?: string; message: string; external?: boolean } | null>(null)
  const [result, setResult] = React.useState<ConvertResult | null>(null)
  const [pending, setPending] = React.useState(false)
  const idem = React.useRef(newRequestId())

  // Campanhas do cliente elegíveis (W-BIZ): basta crm.manage no escopo da proposta — o vendedor não precisa de campaigns.view.
  const campaigns = useQuery({
    queryKey: qk.sub('crm', 'convertible-campaigns', proposal.id),
    queryFn: () => crmApi.convertibleCampaigns(proposal.id),
    enabled: open && !!proposal.org_id,
  })

  React.useEffect(() => {
    if (open) {
      setSelected([])
      setCoupon('')
      setFieldError(null)
      setError(null)
      setResult(null)
      idem.current = newRequestId()
    }
  }, [open])

  const ids = selected
  const idBase = React.useId()
  const readyCount = campaigns.data?.items.filter((c) => c.ready).length ?? 0

  const run = async () => {
    if (!ids.length) {
      setFieldError(
        campaigns.data?.items.length && !readyCount
          ? 'Nenhuma campanha está pronta: resolva as pendências listadas na campanha antes de converter.'
          : 'Escolha ao menos uma campanha montada a partir da proposta.',
      )
      return
    }
    setFieldError(null)
    setError(null)
    setPending(true)
    try {
      const r = await crmApi.convert(proposal.id, { campaign_ids: ids, coupon_code: coupon.trim() || undefined }, idem.current)
      setResult(r)
      if (r.checkout?.error) toast.error(r.checkout.error.message)
      else toast.success('Proposta convertida em pedido.')
      await qc.invalidateQueries({ queryKey: ['crm'] })
      await qc.invalidateQueries({ queryKey: ['campaigns'] })
    } catch (e) {
      const info = paymentErrorInfo(e)
      if (info) setError({ title: info.title, message: info.message, external: info.kind === 'external' })
      else setError({ message: isApiError(e) ? `${e.message}${e.code ? ` (${e.code})` : ''}` : errorMessage(e) })
    } finally {
      setPending(false)
    }
  }

  const co = result?.checkout
  return (
    <Dialog open={open} onOpenChange={(o) => (!pending ? onOpenChange(o) : undefined)}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>Converter {proposal.number} em pedido</DialogTitle>
          <DialogDescription>
            O checkout recalcula o preço no servidor, reserva o inventário por 2 horas e gera a cobrança. O desconto aprovado da proposta é aplicado.
          </DialogDescription>
        </DialogHeader>
        {result ? (
          <div className="flex flex-col gap-4" role="status">
            {co?.error ? (
              <Alert variant="danger">
                <div className="flex min-w-0 flex-1 flex-col gap-1">
                  <AlertTitle>Checkout: {co.error.message}</AlertTitle>
                  <AlertDescription>Código {co.error.code}. O pedido foi registrado sem cobrança; nada ficou reservado.</AlertDescription>
                </div>
              </Alert>
            ) : (
              <Alert variant="success">
                <div className="flex min-w-0 flex-1 flex-col gap-1">
                  <AlertTitle>Pedido criado</AlertTitle>
                  <AlertDescription>Envie ao cliente o PIX gerado ou acompanhe o pagamento em Contas a receber.</AlertDescription>
                </div>
              </Alert>
            )}
            <DetailList
              columns={2}
              items={[
                { label: 'Pedido', value: <span className="font-mono text-xs">{result.order_id}</span>, wide: true },
                { label: 'Total do pedido (checkout)', value: formatCents(result.order_total_cents), numeric: true },
                { label: 'Total da proposta', value: formatCents(result.proposal_total_cents), numeric: true },
                ...(co
                  ? [
                      { label: 'Mídia', value: formatCents(co.media_cents), numeric: true },
                      { label: 'Taxas', value: formatCents(co.fees_cents), numeric: true },
                      { label: 'Desconto', value: formatCents(co.discount_cents), numeric: true },
                      { label: 'Reserva até', value: formatDateTime(co.expires_at) },
                      { label: 'Cobrança', value: co.payment?.status ? <StatusPill map={PAYMENT_STATUS} value={co.payment.status} /> : '—' },
                    ]
                  : []),
              ]}
            />
            {co?.warnings?.length ? (
              <Alert variant="warning">
                <div className="flex min-w-0 flex-1 flex-col gap-1">
                  <AlertTitle>Avisos do checkout</AlertTitle>
                  <AlertDescription>
                    <ul className="m-0 list-disc pl-5">
                      {co.warnings.map((w) => (
                        <li key={w}>{w === 'PAYMENT_CREATION_UNKNOWN' ? 'Cobrança em verificação (resposta do provedor não confirmada).' : w}</li>
                      ))}
                    </ul>
                  </AlertDescription>
                </div>
              </Alert>
            ) : null}
            {result.order_total_cents !== result.proposal_total_cents ? (
              <p className="m-0 text-xs text-text-muted">O total do pedido difere da proposta porque o preço é recalculado no checkout (telas, período e taxas atuais).</p>
            ) : null}
          </div>
        ) : (
          <>
            {error ? (
              <Alert variant={error.external ? 'warning' : 'danger'}>
                <div className="flex min-w-0 flex-1 flex-col gap-1">
                  {error.title ? <AlertTitle>{error.title}</AlertTitle> : null}
                  <AlertDescription>{error.message}</AlertDescription>
                </div>
              </Alert>
            ) : null}
            {!proposal.org_id ? (
              <Alert variant="warning">
                <AlertDescription>Vincule a proposta a uma organização (cliente) antes de converter.</AlertDescription>
              </Alert>
            ) : null}
            <FieldGroup>
              <FieldSet>
                <FieldLegend>Campanhas montadas a partir da proposta</FieldLegend>
                {campaigns.isLoading ? (
                  <Skeleton className="h-20 w-full" />
                ) : campaigns.error ? (
                  <p role="alert" className="m-0 text-sm text-danger">
                    {errorMessage(campaigns.error)}
                  </p>
                ) : !campaigns.data?.items.length ? (
                  <p className="m-0 text-sm text-text-muted">
                    Nenhuma campanha do cliente pronta para conversão (rascunho, ou rejeitada que exige novo pagamento). Monte a campanha antes de converter.
                  </p>
                ) : (
                  <div className="flex flex-col gap-2">
                    {campaigns.data.items.map((c) => (
                      <Field key={c.id} orientation="horizontal" className="min-h-11" disabled={!c.ready}>
                        <Checkbox
                          checked={selected.includes(c.id)}
                          disabled={!c.ready}
                          aria-describedby={c.issues.length ? `${idBase}-${c.id}-issues` : undefined}
                          onCheckedChange={(v) => setSelected((s) => (v ? [...s, c.id] : s.filter((x) => x !== c.id)))}
                        />
                        <FieldLabel className="flex min-w-0 flex-1 flex-wrap items-center gap-2 font-medium">
                          {c.name}
                          <StatusPill map={CAMPAIGN_STATUS} value={c.status} />
                          <span className="text-xs text-text-muted">
                            {c.start_date || c.end_date ? `${formatIsoDate(c.start_date)} a ${formatIsoDate(c.end_date)}` : 'Sem período definido'}
                          </span>
                        </FieldLabel>
                        {c.issues.length ? (
                          <ul id={`${idBase}-${c.id}-issues`} className="m-0 w-full list-disc pl-12 text-xs text-warning">
                            {c.issues.map((i) => (
                              <li key={i.code}>{i.message}</li>
                            ))}
                          </ul>
                        ) : null}
                      </Field>
                    ))}
                  </div>
                )}
                {fieldError ? (
                  <p role="alert" className="m-0 text-xs font-medium text-danger">
                    {fieldError}
                  </p>
                ) : null}
              </FieldSet>
              <Field>
                <FieldLabel>Cupom (opcional)</FieldLabel>
                <Input value={coupon} onChange={(e) => setCoupon(e.target.value)} className="max-w-[220px] uppercase" />
              </Field>
            </FieldGroup>
          </>
        )}
        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)} disabled={pending}>
            {result ? 'Fechar' : 'Cancelar'}
          </Button>
          {!result ? (
            <Button onClick={() => void run()} loading={pending} loadingText="Convertendo…" disabled={!proposal.org_id}>
              Converter e gerar cobrança
            </Button>
          ) : null}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
