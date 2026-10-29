/**
 * Nova campanha — wizard (DS §13; PG-08): identificação → mídia/anúncios → telas/locais por
 * segmentação → período e horários → revisão com preço calculado pelo servidor.
 * Rascunho salvo em cada etapa (`?id=<campanha>&etapa=N` permite continuar depois).
 * Equipe RD e anunciante usam o mesmo fluxo; o anunciante nunca vê nomes de estabelecimentos.
 */
import * as React from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Check } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card } from '@/components/ui/card'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { ErrorState, NoPermission, PageSkeleton } from '@/components/patterns/states'
import { CampaignStatusBadge } from '@/components/campaigns/badges'
import { qk } from '@/lib/queryKeys'
import { isApiError } from '@/lib/errors'
import { cn } from '@/lib/utils'
import { campaignsApi } from './lib/api'
import { useCampaignArea } from './lib/hooks'
import type { CampaignDetail } from './lib/types'
import { StepIdentification } from './wizard/StepIdentification'
import { StepCreatives } from './wizard/StepCreatives'
import { StepTargeting } from './wizard/StepTargeting'
import { StepSchedule } from './wizard/StepSchedule'
import { StepReview } from './wizard/StepReview'

const STEPS = ['Identificação', 'Mídia e anúncios', 'Telas e locais', 'Período e horários', 'Revisão e preço'] as const

export default function CampaignWizardPage() {
  const area = useCampaignArea()
  const [sp, setSp] = useSearchParams()
  const id = sp.get('id')
  const step = Math.min(STEPS.length, Math.max(1, Number(sp.get('etapa')) || 1))
  const headingRef = React.useRef<HTMLHeadingElement>(null)

  const q = useQuery({ queryKey: qk.detail('campaigns', id ?? '-'), queryFn: () => campaignsApi.get(id as string), enabled: !!id })

  const goTo = React.useCallback(
    (n: number, newId?: string) => {
      setSp(
        (prev) => {
          const next = new URLSearchParams(prev)
          if (newId) next.set('id', newId)
          next.set('etapa', String(n))
          return next
        },
        { replace: false },
      )
      requestAnimationFrame(() => headingRef.current?.focus())
    },
    [setSp],
  )

  const onSaved = (c: CampaignDetail | undefined, advance: boolean) => {
    if (!c) return
    if (advance) goTo(Math.min(STEPS.length, step + 1), c.id)
    else if (!id) goTo(step, c.id)
  }

  const breadcrumbs = React.useMemo(() => [{ label: 'Campanhas', to: area.campaignsBase }, { label: 'Nova campanha' }], [area.campaignsBase])

  if (id && q.isLoading) return <PageSkeleton label="Carregando rascunho…" />
  if (id && q.error) {
    if (isApiError(q.error) && q.error.isForbidden) return <NoPermission permission="campaigns.manage" />
    return <ErrorState error={q.error} onRetry={() => void q.refetch()} />
  }
  const campaign = q.data
  const readOnly = !!campaign && campaign.status !== 'RASCUNHO'
  const stepProps = { campaign, area, onSaved, readOnly }

  return (
    <>
      <PageHeader
        title={campaign ? `Campanha: ${campaign.name}` : 'Nova campanha'}
        description={campaign ? 'Rascunho salvo — você pode sair e continuar depois.' : 'Comece pela identificação; o rascunho é salvo a cada etapa.'}
        breadcrumbs={breadcrumbs}
      />
      <div className="flex flex-col gap-6">
        <nav aria-label="Etapas da campanha">
          <ol className="m-0 grid list-none grid-cols-1 gap-2 p-0 sm:grid-cols-5">
            {STEPS.map((label, i) => {
              const n = i + 1
              const current = n === step
              const reachable = n === 1 || !!campaign
              return (
                <li key={label}>
                  <button
                    type="button"
                    onClick={() => reachable && goTo(n)}
                    disabled={!reachable}
                    aria-current={current ? 'step' : undefined}
                    className={cn(
                      'flex min-h-11 w-full items-center gap-2 rounded-lg border px-3 py-2 text-left text-sm transition-colors duration-fast ease-rd',
                      current ? 'border-link bg-surface-hover font-semibold text-text' : 'border-border text-text-muted hover:text-text',
                      !reachable && 'cursor-not-allowed opacity-70',
                    )}
                  >
                    <span
                      className={cn(
                        'grid size-6 shrink-0 place-content-center rounded-full border text-xs font-bold',
                        n < step ? 'border-success bg-success-soft text-success' : current ? 'border-link text-link' : 'border-control-border',
                      )}
                      aria-hidden="true"
                    >
                      {n < step ? <Check className="size-3.5" /> : n}
                    </span>
                    <span>
                      <span className="sr-only">Etapa {n}: </span>
                      {label}
                    </span>
                  </button>
                </li>
              )
            })}
          </ol>
        </nav>

        {readOnly && campaign ? (
          <Alert variant="info">
            <AlertTitle className="flex flex-wrap items-center gap-2">
              Esta campanha não está mais em rascunho <CampaignStatusBadge status={campaign.status} />
            </AlertTitle>
            <AlertDescription>
              As etapas ficam somente para consulta. Mudanças após a compra são feitas pelo pedido de alteração no{' '}
              <Button variant="tertiary" asChild className="h-auto min-h-0 px-0">
                <Link to={`${area.campaignsBase}/${campaign.id}`}>detalhe da campanha</Link>
              </Button>
              .
            </AlertDescription>
          </Alert>
        ) : null}

        <Card>
          <h2 ref={headingRef} tabIndex={-1} className="text-title-section outline-none">
            Etapa {step} de {STEPS.length}: {STEPS[step - 1]}
          </h2>
          {step === 1 ? <StepIdentification key={`s1-${campaign?.id ?? 'new'}`} {...stepProps} /> : null}
          {step === 2 ? <StepCreatives key={`s2-${campaign?.version ?? 0}`} {...stepProps} /> : null}
          {step === 3 ? <StepTargeting key={`s3-${campaign?.id ?? 'new'}`} {...stepProps} /> : null}
          {step === 4 ? <StepSchedule key={`s4-${campaign?.id ?? 'new'}`} {...stepProps} /> : null}
          {step === 5 ? <StepReview {...stepProps} goTo={(n) => goTo(n)} /> : null}
        </Card>
      </div>
    </>
  )
}
