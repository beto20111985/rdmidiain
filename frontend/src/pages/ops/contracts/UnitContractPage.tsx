/**
 * Minha unidade → Contrato: aceite eletrônico do contrato de parceria (e de comodato, quando houver)
 * pelo usuário do ESTABELECIMENTO. Texto completo + checkbox "li e aceito"; após o aceite, mostra o
 * registro (versão, data/hora, IP) e o PDF gerado pelo servidor. O login identifica quem assina.
 * Backend: GET /contracts/templates/current?kind=, POST /contracts/accept, GET /contracts/acceptances.
 */
import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { FileCheck2, FileSignature } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Skeleton } from '@/components/ui/skeleton'
import { Field, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { EmptyState, ErrorState, NoPermission } from '@/components/patterns/states'
import { OptionSelect } from '@/components/ops/filter-select'
import { errorMessage, isApiError } from '@/lib/errors'
import { qk } from '@/lib/queryKeys'
import { formatDateTime } from '@/lib/format'
import { comodatoApi, contractsApi, establishmentsApi } from '@/pages/ops/api/client'
import { CONTRACT_KIND_LABEL } from '@/pages/ops/api/labels'
import type { ContractAcceptance, ContractKind, Establishment } from '@/pages/ops/api/types'
import { AcceptanceList } from '../establishments/tabs/EstablishmentContracts'

function AcceptBox({
  est,
  kind,
  comodatoAgreementId,
  onAccepted,
}: {
  est: Establishment
  kind: ContractKind
  comodatoAgreementId?: string
  onAccepted: (a: ContractAcceptance) => void
}) {
  const [agree, setAgree] = React.useState(false)
  const tpl = useQuery({
    queryKey: qk.sub('establishments', 'contracts', 'current', kind),
    queryFn: () => contractsApi.current(kind),
    retry: false,
  })
  const m = useMutation({
    mutationFn: () =>
      contractsApi.accept({
        template_id: tpl.data!.id,
        org_id: est.org_id,
        establishment_id: est.id,
        comodato_agreement_id: comodatoAgreementId ?? null,
      }),
    onSuccess: (a) => onAccepted(a),
  })
  if (tpl.isLoading) return <Skeleton className="h-64 w-full" />
  if (isApiError(tpl.error) && tpl.error.isNotFound)
    return (
      <Alert variant="info">
        <AlertDescription className="text-text">A RD ainda não publicou este contrato. Você será avisado quando estiver disponível.</AlertDescription>
      </Alert>
    )
  if (tpl.error || !tpl.data) return <ErrorState error={tpl.error} onRetry={() => void tpl.refetch()} size="compact" />
  const t = tpl.data
  return (
    <div className="flex flex-col gap-4">
      <p className="m-0 text-sm text-text-muted">
        {t.title} · versão {t.version}
        {t.published_at ? ` · publicada em ${formatDateTime(t.published_at, est.timezone)}` : ''}
      </p>
      <div
        tabIndex={0}
        role="document"
        aria-label={`Texto do ${CONTRACT_KIND_LABEL[kind]}`}
        className="max-h-[50vh] overflow-y-auto whitespace-pre-wrap rounded-lg border border-border bg-field p-4 text-sm leading-relaxed text-text"
      >
        {t.body}
      </div>
      <Field orientation="horizontal" className="min-h-11">
        <Checkbox checked={agree} onCheckedChange={(c) => setAgree(!!c)} />
        <FieldLabel className="font-normal">
          Li o {CONTRACT_KIND_LABEL[kind].toLowerCase()} (versão {t.version}) e aceito os termos em nome de {est.name}.
        </FieldLabel>
      </Field>
      <p className="m-0 text-xs text-text-muted">
        O aceite registra seu usuário, a data e a hora, o endereço IP e a versão exata do texto, e gera um PDF do contrato aceito.
      </p>
      {m.error ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">
            {isApiError(m.error) && m.error.code === 'IMPERSONATION_FORBIDDEN'
              ? 'O aceite não pode ser feito durante acesso assistido (impersonation). Peça ao responsável que entre com a própria conta.'
              : errorMessage(m.error)}
          </AlertDescription>
        </Alert>
      ) : null}
      <Button className="w-fit" disabled={!agree} loading={m.isPending} loadingText="Registrando aceite…" onClick={() => m.mutate()}>
        <FileSignature aria-hidden="true" />
        Aceitar contrato
      </Button>
    </div>
  )
}

export default function UnitContractPage() {
  const qc = useQueryClient()
  const ests = useQuery({
    queryKey: qk.list('establishments', { ctx: 'unit-contract', page_size: 100 }),
    queryFn: () => establishmentsApi.list({ page_size: 100 }),
  })
  const [estId, setEstId] = React.useState<string | null>(null)
  const [justAccepted, setJustAccepted] = React.useState<ContractAcceptance | null>(null)
  const items = React.useMemo(() => ests.data?.items ?? [], [ests.data])
  React.useEffect(() => {
    if (!estId && items.length) setEstId((items.find((e) => e.onboarding_status === 'AWAITING_CONTRACT') ?? items[0]).id)
  }, [items, estId])
  const est = items.find((e) => e.id === estId) ?? null

  const acc = useQuery({
    queryKey: qk.sub('establishments', 'contracts', 'acceptances', estId ?? ''),
    queryFn: () => contractsApi.acceptances({ org_id: est!.org_id, establishment_id: est!.id, page_size: 50 }),
    enabled: !!est,
  })
  const com = useQuery({
    queryKey: qk.sub('establishments', 'comodato', 'by-establishment', estId ?? ''),
    queryFn: () => comodatoApi.list({ establishment_id: est!.id, status: 'ACTIVE', page_size: 20 }),
    enabled: !!est,
  })

  const onAccepted = (a: ContractAcceptance) => {
    setJustAccepted(a)
    void qc.invalidateQueries({ queryKey: ['establishments'] })
    toast.success(a.establishment_activated ? 'Contrato aceito. Suas telas foram liberadas para anúncios.' : 'Contrato aceito.')
  }

  if (isApiError(ests.error) && ests.error.isForbidden) return <NoPermission permission="establishments.view" />

  const accepted = acc.data?.items ?? []
  const pendingComodato = (com.data?.items ?? []).filter(
    (a) => !accepted.some((x) => x.kind === 'COMODATO' && x.comodato_agreement_id === a.id),
  )

  return (
    <div className="flex flex-col gap-6">
      <PageHeader title="Contrato" description="Contrato de parceria com a RD Mídia Indoor e aceites registrados." />
      {ests.isLoading ? (
        <Skeleton className="h-40 w-full" />
      ) : ests.error ? (
        <ErrorState error={ests.error} onRetry={() => void ests.refetch()} />
      ) : !items.length ? (
        <EmptyState title="Nenhum estabelecimento vinculado ao seu acesso." />
      ) : (
        <>
          {items.length > 1 ? (
            <Card>
              <Field className="max-w-md">
                <FieldLabel>Estabelecimento</FieldLabel>
                <OptionSelect
                  value={estId}
                  onChange={(v) => {
                    setEstId(v)
                    setJustAccepted(null)
                  }}
                  options={items.map((e) => ({ value: e.id, label: `${e.name} — ${e.city}/${e.state}` }))}
                />
              </Field>
            </Card>
          ) : null}
          {est ? (
            <>
              {justAccepted ? (
                <Alert variant="success">
                  <FileCheck2 aria-hidden="true" />
                  <div>
                    <AlertTitle>Aceite registrado</AlertTitle>
                    <AlertDescription>
                      {CONTRACT_KIND_LABEL[justAccepted.kind]} versão {justAccepted.version} aceito em{' '}
                      {formatDateTime(justAccepted.accepted_at, est.timezone)}
                      {justAccepted.ip_address ? ` a partir do IP ${justAccepted.ip_address}` : ''}. O PDF está disponível abaixo.
                    </AlertDescription>
                  </div>
                </Alert>
              ) : null}
              {est.onboarding_status === 'AWAITING_CONTRACT' ? (
                <Card>
                  <CardHeader>
                    <div className="flex flex-col gap-1">
                      <CardTitle>Contrato de parceria — aceite pendente</CardTitle>
                      <CardDescription>As telas de {est.name} começam a exibir anúncios após o aceite.</CardDescription>
                    </div>
                  </CardHeader>
                  <AcceptBox est={est} kind="PARTNERSHIP" onAccepted={onAccepted} />
                </Card>
              ) : null}
              {pendingComodato.map((a) => (
                <Card key={a.id}>
                  <CardHeader>
                    <div className="flex flex-col gap-1">
                      <CardTitle>Contrato de comodato — aceite pendente</CardTitle>
                      <CardDescription>Acordo de comodato iniciado em {formatDateTime(a.started_at, est.timezone)}.</CardDescription>
                    </div>
                  </CardHeader>
                  <AcceptBox est={est} kind="COMODATO" comodatoAgreementId={a.id} onAccepted={onAccepted} />
                </Card>
              ))}
              <Card>
                <CardHeader>
                  <CardTitle>Contratos aceitos</CardTitle>
                </CardHeader>
                {acc.isLoading ? (
                  <Skeleton className="h-24 w-full" />
                ) : acc.error ? (
                  <ErrorState error={acc.error} onRetry={() => void acc.refetch()} size="compact" />
                ) : !accepted.length ? (
                  <EmptyState icon={FileSignature} size="compact" headingLevel="h3" title="Nenhum contrato aceito ainda." />
                ) : (
                  <AcceptanceList items={accepted} timeZone={est.timezone} />
                )}
              </Card>
            </>
          ) : null}
        </>
      )}
    </div>
  )
}
