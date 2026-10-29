/**
 * Carrinho (PG-17; anunciante): várias campanhas em rascunho num único pagamento, cupom,
 * preço SEMPRE do servidor (cotação por campanha + total do pedido calculado no checkout),
 * forma de pagamento (PIX; cartão só com o SDK de tokenização da CloudPayPag — indisponível
 * neste ambiente), Idempotency-Key por tentativa e tratamento de 409/422/503.
 * O carrinho não guarda nada no navegador: a seleção fica na URL (`?c=id1,id2`).
 */
import * as React from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { CreditCard, QrCode, ShoppingCart, Ticket } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState, ErrorState } from '@/components/patterns/states'
import { CampaignStatusBadge, ToneBadge } from '@/components/campaigns/badges'
import { QuoteBreakdown } from '@/components/campaigns/quote-breakdown'
import { qk } from '@/lib/queryKeys'
import { newRequestId } from '@/lib/http'
import { isApiError } from '@/lib/errors'
import { formatBps, formatCents, formatDateTime } from '@/lib/format'
import { campaignsApi, checkoutApi } from './lib/api'
import { ORDER_STATUS } from './lib/labels'
import { formatPlainDate, periodText } from './lib/format'
import { todayIso } from './lib/schedule'
import { useCampaignArea } from './lib/hooks'
import { CheckoutError } from './checkout/shared'
import { describeError } from '@/pages/campaigns/lib/errorText'

export default function CartPage() {
  const area = useCampaignArea()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [sp, setSp] = useSearchParams()
  const selected = React.useMemo(() => (sp.get('c') ?? '').split(',').filter(Boolean), [sp])
  // O cupom aplicado fica no endereço (`cupom`), junto com a seleção (`c`): sobrevive à ida a
  // "Completar meus dados" e à recarga, e é validado de novo no servidor ao voltar.
  const urlCoupon = sp.get('cupom') ?? ''
  const [couponInput, setCouponInput] = React.useState(urlCoupon)
  const [coupon, setCoupon] = React.useState<{ code: string; text: string } | null>(null)
  const [couponError, setCouponError] = React.useState<string | null>(null)
  const [checkingCoupon, setCheckingCoupon] = React.useState(false)
  const [method, setMethod] = React.useState<'pix' | 'card'>('pix')
  const [submitting, setSubmitting] = React.useState(false)
  const [error, setError] = React.useState<unknown>(null)
  const attemptKey = React.useRef<string | null>(null)
  const radioId = React.useId()

  const drafts = useQuery({
    queryKey: qk.list('campaigns', { status: 'RASCUNHO', page_size: 100, cart: true }),
    queryFn: () => campaignsApi.list({ status: 'RASCUNHO', page_size: 100, sort: '-updated_at' }),
  })
  const pendingOrders = useQuery({
    queryKey: qk.list('checkout', { status: 'AGUARDANDO_PAGAMENTO' }),
    queryFn: () => checkoutApi.orders({ status: 'AGUARDANDO_PAGAMENTO', page_size: 20 }),
  })
  const quotes = useQueries({
    queries: selected.map((id) => ({
      queryKey: qk.sub('campaigns', 'detail', id, 'quote', 'cart', coupon?.code ?? ''),
      queryFn: () => campaignsApi.quote(id, coupon?.code ?? null),
      retry: false,
    })),
  })

  const setSelected = (ids: string[]) => {
    attemptKey.current = null
    setError(null)
    setSp(
      (prev) => {
        const next = new URLSearchParams(prev)
        if (ids.length) next.set('c', ids.join(','))
        else next.delete('c')
        return next
      },
      { replace: true },
    )
  }

  const setCouponParam = React.useCallback(
    (code: string | null) =>
      setSp(
        (prev) => {
          const next = new URLSearchParams(prev)
          if (code) next.set('cupom', code)
          else next.delete('cupom')
          return next
        },
        { replace: true },
      ),
    [setSp],
  )

  const checkCoupon = React.useCallback(
    async (raw: string) => {
      const code = raw.trim().toUpperCase()
      if (!code) {
        setCoupon(null)
        setCouponError(null)
        setCouponParam(null)
        return
      }
      setCheckingCoupon(true)
      setCouponError(null)
      try {
        const r = await checkoutApi.validateCoupon(code)
        const text =
          r.discount_type === 'PERCENT'
            ? `${formatBps(r.discount_bps, 0)} de desconto`
            : `${formatCents(r.discount_cents)} de desconto no pedido (rateado entre as campanhas)`
        setCoupon({ code: r.code, text: `${text}${r.min_order_cents ? ` · pedido mínimo ${formatCents(r.min_order_cents)}` : ''} · válido até ${formatDateTime(r.valid_until)}` })
        setCouponParam(r.code)
        attemptKey.current = null
      } catch (err) {
        setCoupon(null)
        setCouponError(describeError(err))
        setCouponParam(null)
      } finally {
        setCheckingCoupon(false)
      }
    },
    [setCouponParam],
  )

  const applyCoupon = (e: React.FormEvent) => {
    e.preventDefault()
    void checkCoupon(couponInput)
  }

  // Volta de "Completar meus dados" (ou recarga): reaplica o cupom do endereço uma única vez.
  const restoredCoupon = React.useRef(false)
  React.useEffect(() => {
    if (restoredCoupon.current) return
    restoredCoupon.current = true
    if (urlCoupon) void checkCoupon(urlCoupon)
  }, [urlCoupon, checkCoupon])

  const anyQuoteError = quotes.some((q) => q.error)
  const quotesLoading = quotes.some((q) => q.isLoading)
  // Total exibido SEMPRE é um valor devolvido pelo servidor: com uma campanha, o total da cotação;
  // com várias, o total do pedido só existe depois que o servidor o calcula no checkout.
  const singleTotal = selected.length === 1 ? (quotes[0]?.data?.quote.total_cents ?? null) : null

  const pay = async () => {
    if (!selected.length || method !== 'pix') return
    setSubmitting(true)
    setError(null)
    // Uma chave por TENTATIVA: repetida só quando a anterior não teve resposta (falha de rede).
    if (!attemptKey.current) attemptKey.current = newRequestId()
    try {
      const r = await checkoutApi.create(selected, coupon?.code ?? null, attemptKey.current)
      attemptKey.current = null
      // Só marca como desatualizado: refazer agora as cotações deste carrinho (que vai desmontar)
      // gastaria chamadas; as telas seguintes buscam de novo ao montar.
      void qc.invalidateQueries({ queryKey: qk.root('campaigns'), refetchType: 'none' })
      void qc.invalidateQueries({ queryKey: qk.root('checkout'), refetchType: 'none' })
      navigate(area.checkoutPath(r.order_id))
    } catch (e) {
      if (!(isApiError(e) && e.isNetwork)) attemptKey.current = null
      setError(e)
    } finally {
      setSubmitting(false)
    }
  }

  const draftItems = drafts.data?.items ?? []
  const today = todayIso()
  return (
    <>
      <PageHeader title="Carrinho" description="Escolha as campanhas em rascunho para pagar juntas. O inventário fica reservado por 2 horas após gerar a cobrança." />
      <div className="flex flex-col gap-6">
        {pendingOrders.data?.items.length ? (
          <Card>
            <CardTitle>Cobranças aguardando pagamento</CardTitle>
            <ul className="m-0 flex list-none flex-col gap-2 p-0">
              {pendingOrders.data.items.map((o) => (
                <li key={o.id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border p-3">
                  <span className="flex flex-wrap items-center gap-2 text-sm">
                    <ToneBadge {...ORDER_STATUS[o.status]} />
                    {formatCents(o.total_cents)} · expira em {formatDateTime(o.expires_at)}
                  </span>
                  <Button variant="secondary" asChild>
                    <Link to={area.checkoutPath(o.id)}>Ver pagamento</Link>
                  </Button>
                </li>
              ))}
            </ul>
          </Card>
        ) : null}

        <div className="grid grid-cols-1 gap-6 xl:grid-cols-12">
          <Card className="xl:col-span-7">
            <CardHeader>
              <CardTitle>Campanhas em rascunho</CardTitle>
              <Button variant="tertiary" asChild>
                <Link to={area.newCampaignPath}>Nova campanha</Link>
              </Button>
            </CardHeader>
            {drafts.isLoading ? (
              <Skeleton className="h-32 w-full" />
            ) : drafts.error ? (
              <ErrorState error={drafts.error} onRetry={() => void drafts.refetch()} size="compact" />
            ) : draftItems.length === 0 ? (
              <EmptyState
                size="compact"
                headingLevel="h3"
                icon={ShoppingCart}
                title="Nenhuma campanha em rascunho"
                description="Crie uma campanha e ela aparece aqui para pagamento."
                action={
                  <Button asChild>
                    <Link to={area.newCampaignPath}>Criar campanha</Link>
                  </Button>
                }
              />
            ) : (
              <ul className="m-0 flex list-none flex-col gap-2 p-0" aria-label="Campanhas disponíveis para pagamento">
                {draftItems.map((c) => {
                  const on = selected.includes(c.id)
                  return (
                    <li key={c.id} className="rounded-lg border border-border p-3">
                      <Field orientation="horizontal" className="min-h-11">
                        <Checkbox checked={on} onCheckedChange={(v) => setSelected(v ? [...selected, c.id] : selected.filter((x) => x !== c.id))} />
                        <FieldLabel className="min-w-0 flex-1 font-semibold [overflow-wrap:anywhere]">{c.name}</FieldLabel>
                        <CampaignStatusBadge status={c.status} />
                      </Field>
                      <p className="m-0 pl-8 text-xs text-text-muted">
                        {periodText(c)} · {c.duration_s} s ·{' '}
                        <Link className="text-link underline-offset-4 hover:underline" to={`${area.newCampaignPath}?id=${c.id}&etapa=5`}>
                          revisar
                        </Link>
                      </p>
                      {c.start_date && c.start_date < today ? (
                        <p className="m-0 pl-8 text-xs font-semibold text-warning" data-testid="cart-start-past">
                          O início ({formatPlainDate(c.start_date)}) já passou — revise as datas antes de pagar.
                        </p>
                      ) : null}
                    </li>
                  )
                })}
              </ul>
            )}
          </Card>

          <Card className="xl:col-span-5">
            <CardTitle>Resumo do pedido</CardTitle>
            {selected.length === 0 ? (
              <p className="m-0 text-sm text-text-muted">Selecione ao menos uma campanha.</p>
            ) : (
              <div className="flex flex-col gap-4">
                {selected.map((id, i) => {
                  const q = quotes[i]
                  const name = draftItems.find((d) => d.id === id)?.name ?? `Campanha ${id.slice(0, 8)}`
                  return (
                    <details key={id} className="rounded-lg border border-border p-3" open={selected.length === 1}>
                      <summary className="flex min-h-11 cursor-pointer flex-wrap items-center justify-between gap-2 text-sm">
                        <span className="font-semibold">{name}</span>
                        <span className="tabular-nums">
                          {q?.isLoading ? 'calculando…' : q?.data ? formatCents(q.data.quote.total_cents) : q?.error ? 'erro' : '—'}
                        </span>
                      </summary>
                      {q?.data ? <QuoteBreakdown quote={q.data.quote} showGroups={false} /> : null}
                      {q?.error ? (
                        <Alert variant="danger">
                          <AlertDescription>
                            {describeError(q.error)}{' '}
                            <Link className="text-link underline" to={`${area.newCampaignPath}?id=${id}&etapa=5`}>
                              Corrigir campanha
                            </Link>
                          </AlertDescription>
                        </Alert>
                      ) : null}
                    </details>
                  )
                })}
                <form className="flex flex-wrap items-end gap-3" onSubmit={applyCoupon}>
                  <Field invalid={!!couponError} className="max-w-xs">
                    <FieldLabel>Cupom</FieldLabel>
                    <Input value={couponInput} onChange={(e) => setCouponInput(e.target.value)} autoComplete="off" />
                    {coupon ? <FieldDescription>{coupon.text}</FieldDescription> : null}
                    <FieldError>{couponError}</FieldError>
                  </Field>
                  <Button type="submit" variant="secondary" loading={checkingCoupon}>
                    <Ticket aria-hidden="true" />
                    Aplicar
                  </Button>
                </form>
                {selected.length === 1 ? (
                  <div className="flex items-baseline justify-between border-t border-border pt-3">
                    <span className="text-sm font-semibold">Total (calculado pelo servidor)</span>
                    <span className="text-xl font-bold tabular-nums" data-testid="cart-total">
                      {singleTotal === null ? '—' : formatCents(singleTotal)}
                    </span>
                  </div>
                ) : null}
                <p className="m-0 text-xs text-text-muted">
                  {selected.length > 1
                    ? 'Com várias campanhas, o total do pedido (inclusive o rateio de cupom de valor fixo) é calculado e congelado pelo servidor ao gerar a cobrança.'
                    : 'O valor é congelado pelo servidor ao gerar a cobrança.'}
                </p>

                <fieldset className="m-0 flex flex-col gap-2 border-0 p-0">
                  <legend className="mb-1 text-sm font-semibold">Forma de pagamento</legend>
                  <RadioGroup aria-label="Forma de pagamento" value={method} onValueChange={(v) => setMethod(v as 'pix' | 'card')}>
                    <Field orientation="horizontal" className="min-h-11">
                      <RadioGroupItem id={`${radioId}-pix`} value="pix" />
                      <FieldLabel className="font-normal" htmlFor={`${radioId}-pix`}>
                        <QrCode aria-hidden="true" className="mr-1 inline size-4" />
                        PIX (QR code e copia e cola)
                      </FieldLabel>
                    </Field>
                    <Field orientation="horizontal" className="min-h-11" disabled>
                      <RadioGroupItem id={`${radioId}-card`} value="card" disabled />
                      <FieldLabel className="font-normal" htmlFor={`${radioId}-card`}>
                        <CreditCard aria-hidden="true" className="mr-1 inline size-4" />
                        Cartão de crédito
                      </FieldLabel>
                    </Field>
                  </RadioGroup>
                  <p className="m-0 text-xs text-text-muted" data-testid="card-unavailable">
                    Pagamento por cartão indisponível neste ambiente (o SDK de tokenização da CloudPayPag não está configurado).
                  </p>
                </fieldset>

                <CheckoutError error={error} />
                <Button onClick={() => void pay()} loading={submitting} disabled={!selected.length || quotesLoading || anyQuoteError || method !== 'pix'} size="block">
                  Gerar cobrança PIX
                </Button>
                <p className="m-0 text-xs text-text-muted">Pagamento não aprova a campanha: após a confirmação ela vai para a análise da RD.</p>
              </div>
            )}
          </Card>
        </div>
      </div>
    </>
  )
}
