/**
 * Etapa 5 — Revisão: resumo + PREÇO CALCULADO PELO SERVIDOR (`POST /campaigns/{id}/quote`)
 * com detalhamento e disponibilidade. Anunciante segue para o carrinho/pagamento; a equipe
 * RD abre o detalhe da campanha (o pagamento é do cliente ou por proposta comercial).
 */
import * as React from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowRight, ShoppingCart } from 'lucide-react'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { ErrorState } from '@/components/patterns/states'
import { QuoteBreakdown } from '@/components/campaigns/quote-breakdown'
import { qk } from '@/lib/queryKeys'
import { isApiError } from '@/lib/errors'
import { campaignsApi } from '../lib/api'
import { EXCLUSIVITY_LABEL, PRIORITY_LABEL } from '../lib/labels'
import { bandsText, daysText, frequencyText, periodText, targetText } from '../lib/format'
import { ERROR_FIELD } from '../lib/rules'
import { describeError } from '../lib/errorText'
import { AvailabilityPanel } from './StepTargeting'
import type { StepProps } from './shared'

const FIELD_STEP: Record<string, number> = {
  name: 1,
  duration_s: 2,
  targets: 3,
  start_date: 4,
  end_date: 4,
  time_bands: 4,
  specific_times: 4,
  freq_value: 4,
  days_mask: 4,
}

export function StepReview({ campaign, area, goTo }: StepProps & { goTo: (step: number) => void }) {
  const navigate = useNavigate()
  const [coupon, setCoupon] = React.useState('')
  const [appliedCoupon, setAppliedCoupon] = React.useState<string | null>(null)
  const quote = useQuery({
    queryKey: qk.sub('campaigns', 'detail', campaign?.id ?? '-', 'quote', campaign?.version ?? 0, appliedCoupon ?? ''),
    queryFn: () => campaignsApi.quote(campaign!.id, appliedCoupon),
    enabled: !!campaign,
    retry: false,
  })
  if (!campaign) return <Alert variant="info"><AlertDescription>Salve a identificação primeiro.</AlertDescription></Alert>

  const links = campaign.creatives.filter((c) => !c.pending_removal)
  const errField = isApiError(quote.error) ? ERROR_FIELD[quote.error.code] : undefined
  const fixStep = errField ? FIELD_STEP[errField] : undefined
  const summary: Array<[string, React.ReactNode, number]> = [
    ['Campanha', campaign.name, 1],
    ['Duração', `${campaign.duration_s} s`, 2],
    ['Anúncios', links.length ? links.map((l) => l.code).join(', ') : 'Nenhum (enviar depois do pagamento)', 2],
    ['Segmentação', campaign.targets.length ? campaign.targets.map((t) => targetText(t)).join(' · ') : '—', 3],
    ['Período', periodText(campaign), 4],
    ['Dias', daysText(campaign.days_mask), 4],
    ['Faixas', bandsText(campaign.time_bands), 4],
    ['Frequência', frequencyText(campaign), 4],
    ['Prioridade', `${PRIORITY_LABEL[campaign.priority]} · ${EXCLUSIVITY_LABEL[campaign.exclusivity]}`, 4],
  ]

  return (
    <div className="flex flex-col gap-6">
      <dl className="m-0 grid grid-cols-1 gap-3 md:grid-cols-2">
        {summary.map(([k, v, step]) => (
          <div key={k} className="flex items-start justify-between gap-3 rounded-lg border border-border p-3">
            <div className="flex min-w-0 flex-col">
              <dt className="text-xs text-text-muted">{k}</dt>
              <dd className="m-0 break-words text-sm">{v}</dd>
            </div>
            <Button variant="tertiary" onClick={() => goTo(step)} aria-label={`Editar ${k.toLowerCase()}`}>
              Editar
            </Button>
          </div>
        ))}
      </dl>

      {!area.isStaff ? (
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(e) => {
            e.preventDefault()
            setAppliedCoupon(coupon.trim() ? coupon.trim().toUpperCase() : null)
          }}
        >
          <Field className="max-w-xs">
            <FieldLabel>Cupom (opcional)</FieldLabel>
            <Input value={coupon} onChange={(e) => setCoupon(e.target.value)} autoComplete="off" />
            <FieldDescription>O desconto é validado e calculado pelo servidor.</FieldDescription>
          </Field>
          <Button type="submit" variant="secondary">
            Aplicar cupom
          </Button>
        </form>
      ) : null}

      <section className="flex flex-col gap-4" aria-labelledby="price-title">
        <h3 id="price-title" className="text-title-section">
          Preço
        </h3>
        {quote.isLoading ? (
          <Skeleton className="h-48 w-full" />
        ) : quote.error ? (
          <div className="flex flex-col gap-3">
            <ErrorState
              error={quote.error}
              title="Não foi possível calcular o preço"
              description={describeError(quote.error)}
              onRetry={() => void quote.refetch()}
              size="compact"
            />
            {fixStep ? (
              <Button variant="secondary" className="w-fit" onClick={() => goTo(fixStep)}>
                Corrigir na etapa {fixStep}
              </Button>
            ) : null}
          </div>
        ) : quote.data ? (
          <>
            <QuoteBreakdown quote={quote.data.quote} />
            <h4 className="text-title-card">Disponibilidade</h4>
            <AvailabilityPanel data={quote.data.availability} />
          </>
        ) : null}
      </section>

      {campaign.status === 'RASCUNHO' ? (
        <Alert variant="info">
          <AlertTitle>Próximo passo</AlertTitle>
          <AlertDescription>
            {area.isStaff
              ? 'A campanha fica em rascunho. O pagamento é feito pelo anunciante no painel dele (ou por proposta comercial).'
              : 'Ao gerar o pagamento, o inventário fica reservado por 2 horas. Pagamento não aprova a campanha: ela passa pela análise da RD.'}
          </AlertDescription>
        </Alert>
      ) : null}

      <div className="flex flex-wrap justify-end gap-3 border-t border-border pt-4">
        <Button variant="secondary" asChild>
          <Link to={`${area.campaignsBase}/${campaign.id}`}>Abrir campanha</Link>
        </Button>
        {!area.isStaff && campaign.status === 'RASCUNHO' ? (
          <Button onClick={() => navigate(`${area.cartPath}?c=${campaign.id}`)} disabled={!quote.data}>
            <ShoppingCart aria-hidden="true" />
            Ir para o pagamento
            <ArrowRight aria-hidden="true" />
          </Button>
        ) : null}
      </div>
    </div>
  )
}
