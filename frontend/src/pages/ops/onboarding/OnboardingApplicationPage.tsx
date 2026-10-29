/**
 * Cadastro de parceiro (equipe RD): wizard por etapas — empresa → estabelecimento e categorias →
 * endereço → telas → comercial → técnico/instalação → documentos → revisão/envio — e aprovação
 * (RD global): aprovar cria empresa + estabelecimento (aguardando contrato); recusar exige motivo.
 * Depois: convite do responsável (Usuários), aceite do contrato e cadastro/pareamento das telas.
 */
import * as React from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Check, CheckCircle2, Circle, Send, ThumbsDown, ThumbsUp } from 'lucide-react'
import { Stepper } from '@/components/ops/stepper'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { ConfirmDialog } from '@/components/patterns/confirm-dialog'
import { ErrorState, NoPermission, PageSkeleton } from '@/components/patterns/states'
import { ReasonDialog } from '@/components/ops/reason-dialog'
import { useCan } from '@/hooks/usePermissions'
import { errorMessage, isApiError } from '@/lib/errors'
import { qk } from '@/lib/queryKeys'
import { formatDateTime } from '@/lib/format'
import { onboardingApi } from '@/pages/ops/api/client'
import { ONBOARDING_ORIGIN_LABEL, ONBOARDING_STATUS_LABEL, ONBOARDING_STEP_LABEL, labelOf } from '@/pages/ops/api/labels'
import type { OnboardingApplication, OnboardingStepKey } from '@/pages/ops/api/types'
import {
  AddressStepForm,
  CommercialStepForm,
  CompanyStepForm,
  EstablishmentStepForm,
  InstallationStepForm,
  ScreensStepForm,
} from './stepForms'
import { DocumentsPanel } from './DocumentsPanel'
import { onboardingStatusTone } from './stepRules'

type WizardStep = OnboardingStepKey | 'documents' | 'contract' | 'review'
const ORDER: WizardStep[] = ['company', 'establishment', 'address', 'screens', 'commercial', 'installation', 'documents', 'contract', 'review']
const EXTRA_LABEL: Record<string, string> = { documents: 'Documentos', contract: 'Contrato e aceite', review: 'Revisão e envio' }
const FORMS = {
  company: CompanyStepForm,
  establishment: EstablishmentStepForm,
  address: AddressStepForm,
  screens: ScreensStepForm,
  commercial: CommercialStepForm,
  installation: InstallationStepForm,
} as const

export default function OnboardingApplicationPage() {
  const { id = '' } = useParams()
  const can = useCan()
  const qc = useQueryClient()
  const [sp, setSp] = useSearchParams()
  const [approveOpen, setApproveOpen] = React.useState(false)
  const [rejectOpen, setRejectOpen] = React.useState(false)
  const [approved, setApproved] = React.useState<OnboardingApplication | null>(null)
  const key = qk.sub('establishments', 'onboarding', 'detail', id)
  const q = useQuery({ queryKey: key, queryFn: () => onboardingApi.get(id) })
  const a = q.data

  const step = (ORDER.includes(sp.get('etapa') as WizardStep) ? sp.get('etapa') : 'company') as WizardStep
  const goto = (s: WizardStep) =>
    setSp(
      (p) => {
        const n = new URLSearchParams(p)
        n.set('etapa', s)
        return n
      },
      { replace: true },
    )
  const next = () => {
    const i = ORDER.indexOf(step)
    if (i < ORDER.length - 1) goto(ORDER[i + 1])
  }
  const save = async (s: OnboardingStepKey, data: Record<string, unknown>) => {
    const r = await onboardingApi.saveStep(id, s, data)
    qc.setQueryData(key, (old: OnboardingApplication | undefined) => ({ ...r, documents: old?.documents ?? r.documents }))
    toast.success(`Etapa “${ONBOARDING_STEP_LABEL[s]}” salva.`)
    next()
  }
  const submit = useMutation({
    mutationFn: () => onboardingApi.submit(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['establishments'] })
      toast.success('Cadastro enviado para aprovação.')
    },
  })
  const reject = useMutation({
    mutationFn: (reason: string) => onboardingApi.reject(id, reason),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['establishments'] })
      toast.success('Cadastro recusado. O responsável foi avisado por e-mail.')
    },
  })

  const crumbs = React.useMemo(
    () => [
      { label: 'Estabelecimentos', to: '/estabelecimentos' },
      { label: 'Cadastros', to: '/estabelecimentos/cadastros' },
      { label: a?.email ?? 'Cadastro' },
    ],
    [a?.email],
  )

  if (q.isLoading) return <PageSkeleton />
  if (isApiError(q.error) && q.error.isForbidden) return <NoPermission permission="establishments.manage" />
  if (q.error || !a) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />

  const editable = a.status === 'INVITED' || a.status === 'IN_PROGRESS'
  const stepInfo = (s: WizardStep) => a.steps.find((x) => x.key === s)
  const missing = a.steps.filter((s) => s.required && !s.completed)
  const completed = (s: WizardStep) =>
    s === 'documents' ? !!a.documents?.length : s === 'review' ? a.status !== 'INVITED' && a.status !== 'IN_PROGRESS' : s === 'contract' ? a.status === 'APPROVED' : !!stepInfo(s)?.completed
  const estName = (a.data.establishment as { name?: string } | undefined)?.name

  let body: React.ReactNode
  if (step in FORMS) {
    const Form = FORMS[step as keyof typeof FORMS]
    body = (
      <Form
        key={`${step}-${a.status}`}
        initial={a.data[step as OnboardingStepKey]}
        staff
        disabled={!editable}
        onSave={(d) => save(step as OnboardingStepKey, d)}
      />
    )
  } else if (step === 'documents') {
    body = (
      <DocumentsPanel
        documents={a.documents ?? []}
        disabled={!editable}
        upload={(kind, file, onProgress) => onboardingApi.uploadDocument(id, kind, file, onProgress)}
        onUploaded={() => void q.refetch()}
      />
    )
  } else if (step === 'contract') {
    body = (
      <div className="flex flex-col gap-3 text-sm text-text">
        <p className="m-0">
          O contrato de parceria é aceito eletronicamente pelo responsável do estabelecimento no primeiro acesso ao painel
          (“Minha unidade → Contrato”), com registro de versão, IP e data/hora. Contrato assinado em papel pode ser registrado
          pela equipe RD no detalhe do estabelecimento (aba Contratos).
        </p>
        <p className="m-0 text-text-muted">
          Até o aceite, o estabelecimento fica “Aguardando aceite do contrato” e suas telas não entram no inventário vendável.
        </p>
      </div>
    )
  } else {
    body = (
      <div className="flex flex-col gap-4">
        <ul className="m-0 flex list-none flex-col gap-2 p-0">
          {a.steps.map((s) => (
            <li key={s.key} className="flex items-center gap-2 text-sm">
              {s.completed ? <Check aria-hidden="true" className="size-4 text-success" /> : <Circle aria-hidden="true" className="size-4 text-text-muted" />}
              <span className="text-text">{ONBOARDING_STEP_LABEL[s.key]}</span>
              <span className="text-text-muted">{s.required ? '(obrigatória)' : '(opcional)'}</span>
              {!s.completed && editable ? (
                <Button variant="tertiary" onClick={() => goto(s.key)}>
                  Preencher
                </Button>
              ) : null}
            </li>
          ))}
        </ul>
        {missing.length && editable ? (
          <Alert variant="warning">
            <AlertDescription className="text-text">
              Preencha as etapas obrigatórias: {missing.map((m) => ONBOARDING_STEP_LABEL[m.key]).join(', ')}.
            </AlertDescription>
          </Alert>
        ) : null}
        {submit.error ? (
          <Alert variant="danger">
            <AlertDescription className="text-text">{errorMessage(submit.error)}</AlertDescription>
          </Alert>
        ) : null}
        {editable ? (
          <Button className="w-fit" onClick={() => submit.mutate()} disabled={missing.length > 0} loading={submit.isPending}>
            <Send aria-hidden="true" />
            Enviar para aprovação
          </Button>
        ) : null}
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title={estName ? `Cadastro: ${estName}` : `Cadastro de ${a.email}`}
        description={`${labelOf(ONBOARDING_ORIGIN_LABEL, a.origin)} · criado em ${formatDateTime(a.created_at)}`}
        breadcrumbs={crumbs}
        actions={
          a.status === 'SUBMITTED' && can('establishments.manage') ? (
            <div className="flex flex-wrap gap-2">
              <Button variant="destructive" onClick={() => setRejectOpen(true)}>
                <ThumbsDown aria-hidden="true" />
                Recusar
              </Button>
              <Button onClick={() => setApproveOpen(true)}>
                <ThumbsUp aria-hidden="true" />
                Aprovar
              </Button>
            </div>
          ) : undefined
        }
      />
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={onboardingStatusTone(a.status)}>{labelOf(ONBOARDING_STATUS_LABEL, a.status)}</Badge>
        <span className="text-sm text-text-muted">{a.email}</span>
        {a.token_expires_at && editable ? (
          <span className="text-xs text-text-muted">Link do responsável válido até {formatDateTime(a.token_expires_at)}</span>
        ) : null}
      </div>

      {a.status === 'APPROVED' || approved ? (
        <Alert variant="success">
          <CheckCircle2 aria-hidden="true" />
          <div className="flex flex-col gap-2">
            <AlertTitle>Cadastro aprovado</AlertTitle>
            <AlertDescription>
              Próximos passos: convidar o responsável com o perfil “Estabelecimento” (Usuários), aguardar o aceite do contrato e
              cadastrar/parear as telas.
            </AlertDescription>
            <div className="flex flex-wrap gap-2">
              {(approved ?? a).establishment_id ? (
                <Button asChild>
                  <Link to={`/estabelecimentos/${(approved ?? a).establishment_id}`}>Abrir estabelecimento</Link>
                </Button>
              ) : null}
              {can('users.manage') ? (
                <Button asChild variant="secondary">
                  <Link to="/usuarios">Convidar responsável</Link>
                </Button>
              ) : null}
            </div>
          </div>
        </Alert>
      ) : a.status === 'REJECTED' ? (
        <Alert variant="danger">
          <AlertTitle>Cadastro recusado em {formatDateTime(a.decided_at)}</AlertTitle>
          <AlertDescription>Motivo: {a.decision_reason ?? '—'}</AlertDescription>
        </Alert>
      ) : a.status === 'SUBMITTED' ? (
        <Alert variant="info">
          <AlertDescription className="text-text">Enviado em {formatDateTime(a.submitted_at)}. Revise as etapas e aprove ou recuse.</AlertDescription>
        </Alert>
      ) : null}

      <div className="grid grid-cols-1 gap-6 md:grid-cols-[240px_1fr]">
        <Card className="h-fit p-3 max-md:p-3">
          <Stepper
            steps={ORDER.map((k) => ({ key: k, label: ONBOARDING_STEP_LABEL[k] ?? EXTRA_LABEL[k] ?? k }))}
            current={step}
            onSelect={(k) => goto(k as WizardStep)}
            completed={(k) => completed(k as WizardStep)}
          />
        </Card>
        <Card>
          <CardHeader>
            <div className="flex flex-col gap-1">
              <CardTitle>{ONBOARDING_STEP_LABEL[step] ?? EXTRA_LABEL[step]}</CardTitle>
              {stepInfo(step)?.staff_only ? <CardDescription>Etapa preenchida somente pela equipe RD.</CardDescription> : null}
              {!editable && step in FORMS ? <CardDescription>Cadastro enviado: os dados ficam somente para leitura.</CardDescription> : null}
            </div>
          </CardHeader>
          {body}
        </Card>
      </div>

      <ConfirmDialog
        open={approveOpen}
        onOpenChange={setApproveOpen}
        title={`Aprovar o cadastro${estName ? ` de ${estName}` : ''}?`}
        consequences={[
          'Cria (ou vincula) a empresa e o estabelecimento com os dados informados.',
          'O estabelecimento fica aguardando o aceite do contrato de parceria.',
          'A decisão é registrada na auditoria.',
        ]}
        confirmLabel="Aprovar cadastro"
        onConfirm={async () => {
          const r = await onboardingApi.approve(id)
          setApproved(r)
          void qc.invalidateQueries({ queryKey: ['establishments'] })
          toast.success('Cadastro aprovado.')
        }}
      />
      <ReasonDialog
        open={rejectOpen}
        onOpenChange={setRejectOpen}
        title="Recusar o cadastro?"
        description="O responsável recebe o motivo por e-mail."
        confirmLabel="Recusar cadastro"
        destructive
        onConfirm={(reason) => reject.mutateAsync(reason)}
      />
    </div>
  )
}
