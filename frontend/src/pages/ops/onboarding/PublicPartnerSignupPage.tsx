/**
 * Página PÚBLICA de auto-cadastro do estabelecimento parceiro (/cadastro-parceiro).
 * - Sem token: pede o e-mail (POST /onboarding/public/signup, resposta sempre neutra).
 * - Com `?token=` (link do e-mail): o token é lido UMA vez, removido da barra de endereço e mantido
 *   só em memória; vai SEMPRE no header `X-RD-Onboarding-Token` (nunca na URL da API, nunca em
 *   storage). Wizard: empresa → estabelecimento → endereço → telas → documentos → contrato → envio.
 */
import * as React from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, MailCheck, Send } from 'lucide-react'
import { AuthLayout } from '@/features/auth/components/AuthLayout'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Skeleton } from '@/components/ui/skeleton'
import { Stepper } from '@/components/ops/stepper'
import { useOneTimeToken } from '@/components/ops/use-one-time-token'
import { errorMessage, isApiError } from '@/lib/errors'
import { qk } from '@/lib/queryKeys'
import { formatDateTime } from '@/lib/format'
import { publicOnboardingApi } from '@/pages/ops/api/client'
import { ONBOARDING_STATUS_LABEL, ONBOARDING_STEP_LABEL, labelOf } from '@/pages/ops/api/labels'
import type { OnboardingApplication, OnboardingStepKey } from '@/pages/ops/api/types'
import { AddressStepForm, CompanyStepForm, EstablishmentStepForm, ScreensStepForm } from './stepForms'
import { DocumentsPanel } from './DocumentsPanel'

type PublicStep = 'company' | 'establishment' | 'address' | 'screens' | 'documents' | 'contract' | 'review'
const ORDER: PublicStep[] = ['company', 'establishment', 'address', 'screens', 'documents', 'contract', 'review']
const EXTRA: Record<string, string> = { documents: 'Documentos (opcional)', contract: 'Contrato', review: 'Revisão e envio' }
const FORMS = { company: CompanyStepForm, establishment: EstablishmentStepForm, address: AddressStepForm, screens: ScreensStepForm } as const
const KEY = qk.sub('establishments', 'public-onboarding')

function SignupForm() {
  const [email, setEmail] = React.useState('')
  const [touched, setTouched] = React.useState(false)
  const valid = /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email.trim())
  const m = useMutation({ mutationFn: () => publicOnboardingApi.signup(email.trim().toLowerCase()) })
  if (m.isSuccess)
    return (
      <Alert variant="success">
        <MailCheck aria-hidden="true" />
        <div>
          <AlertTitle>Verifique seu e-mail</AlertTitle>
          <AlertDescription>
            Se o e-mail for válido, você receberá um link para preencher o cadastro (válido por 7 dias). Confira também a caixa de spam.
          </AlertDescription>
        </div>
      </Alert>
    )
  const captcha = isApiError(m.error) && m.error.code === 'CAPTCHA_REQUIRED'
  return (
    <form
      noValidate
      className="flex flex-col gap-5"
      onSubmit={(e) => {
        e.preventDefault()
        setTouched(true)
        if (valid) m.mutate()
      }}
    >
      <Field invalid={touched && !valid} required>
        <FieldLabel>E-mail do responsável</FieldLabel>
        <Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" />
        <FieldDescription>Enviaremos um link seguro para você preencher os dados do estabelecimento.</FieldDescription>
        <FieldError>{touched && !valid ? 'Informe um e-mail válido.' : null}</FieldError>
      </Field>
      {m.error ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">
            {captcha
              ? 'A verificação anti-robô não pôde ser concluída nesta página. Tente mais tarde ou fale com o comercial da RD.'
              : errorMessage(m.error)}
          </AlertDescription>
        </Alert>
      ) : null}
      <Button type="submit" loading={m.isPending} className="w-full">
        Receber link de cadastro
      </Button>
    </form>
  )
}

function Wizard({ token, app }: { token: string; app: OnboardingApplication }) {
  const qc = useQueryClient()
  const [step, setStep] = React.useState<PublicStep>(() => {
    const firstOpen = app.steps.find((s) => !s.completed)?.key as PublicStep | undefined
    return firstOpen ?? 'review'
  })
  const editable = app.status === 'INVITED' || app.status === 'IN_PROGRESS'
  const next = () => setStep((s) => ORDER[Math.min(ORDER.indexOf(s) + 1, ORDER.length - 1)])
  const save = async (s: OnboardingStepKey, data: Record<string, unknown>) => {
    const r = await publicOnboardingApi.saveStep(token, s, data)
    qc.setQueryData(KEY, (old: OnboardingApplication | undefined) => ({ ...r, documents: old?.documents ?? r.documents }))
    next()
  }
  const submit = useMutation({
    mutationFn: () => publicOnboardingApi.submit(token),
    onSuccess: (r) => qc.setQueryData(KEY, (old: OnboardingApplication | undefined) => ({ ...r, documents: old?.documents })),
  })
  const missing = app.steps.filter((s) => s.required && !s.completed)

  if (!editable)
    return (
      <Alert variant={app.status === 'REJECTED' ? 'danger' : 'success'}>
        <CheckCircle2 aria-hidden="true" />
        <div>
          <AlertTitle>{labelOf(ONBOARDING_STATUS_LABEL, app.status)}</AlertTitle>
          <AlertDescription>
            {app.status === 'SUBMITTED'
              ? `Cadastro enviado em ${formatDateTime(app.submitted_at)}. A equipe RD vai analisar e entrar em contato pelo e-mail ${app.email}.`
              : app.status === 'APPROVED'
                ? 'Cadastro aprovado. Você receberá um convite para acessar o painel e aceitar o contrato de parceria.'
                : app.status === 'REJECTED'
                  ? `Cadastro não aprovado. ${app.decision_reason ?? ''}`
                  : 'Este cadastro não está mais aberto para edição.'}
          </AlertDescription>
        </div>
      </Alert>
    )

  let body: React.ReactNode
  if (step in FORMS) {
    const Form = FORMS[step as keyof typeof FORMS]
    body = <Form key={step} initial={app.data[step as OnboardingStepKey]} staff={false} onSave={(d) => save(step as OnboardingStepKey, d)} />
  } else if (step === 'documents') {
    body = (
      <div className="flex flex-col gap-4">
        <DocumentsPanel
          documents={app.documents ?? []}
          upload={(kind, file) => publicOnboardingApi.uploadDocument(token, kind, file)}
          onUploaded={() => void qc.invalidateQueries({ queryKey: KEY })}
        />
        <Button variant="secondary" className="w-fit" onClick={next}>
          Continuar
        </Button>
      </div>
    )
  } else if (step === 'contract') {
    body = (
      <div className="flex flex-col gap-4 text-sm text-text">
        <p className="m-0">
          Depois da aprovação, você recebe o convite de acesso ao painel. No primeiro acesso, o contrato de parceria fica
          disponível para leitura e aceite eletrônico (com registro de data, hora e IP, e cópia em PDF).
        </p>
        <Button variant="secondary" className="w-fit" onClick={next}>
          Entendi, continuar
        </Button>
      </div>
    )
  } else {
    body = (
      <div className="flex flex-col gap-4">
        {missing.length ? (
          <Alert variant="warning">
            <AlertDescription className="text-text">
              Falta preencher: {missing.map((m) => ONBOARDING_STEP_LABEL[m.key]).join(', ')}.
            </AlertDescription>
          </Alert>
        ) : (
          <p className="m-0 text-sm text-text">Tudo pronto. Confira os dados e envie o cadastro para a análise da equipe RD.</p>
        )}
        <ReviewSummary app={app} />
        {submit.error ? (
          <Alert variant="danger">
            <AlertDescription className="text-text">{errorMessage(submit.error)}</AlertDescription>
          </Alert>
        ) : null}
        <Button className="w-fit" onClick={() => submit.mutate()} disabled={missing.length > 0} loading={submit.isPending}>
          <Send aria-hidden="true" />
          Enviar cadastro
        </Button>
      </div>
    )
  }

  const done = (k: string) =>
    k === 'documents' ? !!app.documents?.length : k === 'contract' || k === 'review' ? false : !!app.steps.find((s) => s.key === k)?.completed
  return (
    <div className="flex flex-col gap-6">
      <Stepper steps={ORDER.map((k) => ({ key: k, label: ONBOARDING_STEP_LABEL[k] ?? EXTRA[k] }))} current={step} onSelect={(k) => setStep(k as PublicStep)} completed={done} />
      <section aria-labelledby="etapa-atual" className="flex flex-col gap-4">
        <h2 id="etapa-atual" className="m-0 text-title-section text-text">
          {ONBOARDING_STEP_LABEL[step] ?? EXTRA[step]}
        </h2>
        {body}
      </section>
    </div>
  )
}

const txt = (v: unknown): string | null => (typeof v === 'string' && v.trim() ? v.trim() : typeof v === 'number' ? String(v) : null)

/** Resumo do que será enviado (somente o que o próprio responsável preencheu). */
function ReviewSummary({ app }: { app: OnboardingApplication }) {
  const c = app.data.company ?? {}
  const e = app.data.establishment ?? {}
  const a = app.data.address ?? {}
  const sc = app.data.screens ?? {}
  const city = [txt(a.city), txt(a.state)].filter(Boolean).join('/')
  const street = [txt(a.address), txt(a.number)].filter(Boolean).join(', ')
  const rows: Array<[string, string | null]> = [
    ['Empresa', txt(c.name)],
    ['Estabelecimento', txt(e.name)],
    ['Endereço', [street, city].filter(Boolean).join(' — ') || null],
    ['Telas planejadas', txt(sc.planned_screens)],
    ['Documentos enviados', String(app.documents?.length ?? 0)],
    ['E-mail de contato', app.email],
  ]
  return (
    <dl className="m-0 grid grid-cols-1 gap-x-6 gap-y-2 rounded-lg border border-border p-4 text-sm sm:grid-cols-[max-content_1fr]">
      {rows.map(([k, v]) => (
        <React.Fragment key={k}>
          <dt className="text-text-muted">{k}</dt>
          <dd className="m-0 break-words text-text">{v ?? '—'}</dd>
        </React.Fragment>
      ))}
    </dl>
  )
}

export default function PublicPartnerSignupPage() {
  const token = useOneTimeToken()
  const q = useQuery({
    queryKey: KEY,
    queryFn: () => publicOnboardingApi.get(token!),
    enabled: !!token,
    retry: false,
    staleTime: Infinity,
  })
  const invalid = isApiError(q.error) && (q.error.status === 401 || q.error.status === 404)

  return (
    <AuthLayout
      wide
      title="Seja um estabelecimento parceiro"
      description="Receba TVs com anúncios e conteúdo do seu negócio e ganhe comissão pelas exibições."
      footer={
        <Link to="/login" className="text-link underline-offset-4 hover:underline">
          Já tenho acesso ao painel
        </Link>
      }
    >
      {!token ? (
        <SignupForm />
      ) : q.isLoading ? (
        <div role="status" className="flex flex-col gap-3">
          <span className="sr-only">Carregando seu cadastro…</span>
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-40 w-full" />
        </div>
      ) : invalid ? (
        <div className="flex flex-col gap-5">
          <Alert variant="warning">
            <AlertTitle>Link inválido ou expirado</AlertTitle>
            <AlertDescription>O link de cadastro vale por 7 dias. Informe seu e-mail para receber um novo.</AlertDescription>
          </Alert>
          <SignupForm />
        </div>
      ) : q.error || !q.data ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">{errorMessage(q.error)}</AlertDescription>
        </Alert>
      ) : (
        <Wizard token={token} app={q.data} />
      )}
    </AuthLayout>
  )
}
