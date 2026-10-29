/**
 * Página PÚBLICA de auto-cadastro do ANUNCIANTE (/cadastro-anunciante; REQ-ONB-004, E2 #1017–#1022).
 * 1) Sem token: e-mail (+ CAPTCHA quando o servidor exigir) → resposta sempre neutra (não revela conta).
 * 2) Com `?token=` do e-mail (confirmação do endereço): token lido uma vez, removido da URL, só em memória,
 *    enviado só em `X-RD-Signup-Token`. Etapas: empresa (CPF/CNPJ, inclusive alfanumérico) → segmentos →
 *    acesso (nome e senha com a política do servidor) → termos/privacidade (com versão) → revisão.
 * 3) Concluído: sessão aberta no contexto do anunciante e duas opções — criar campanha (self-service) ou
 *    falar com o comercial (POST /advertiser-signup/sales-contact).
 */
import * as React from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { CheckCircle2, Headset, MailCheck, Megaphone, Send } from 'lucide-react'
import { AuthLayout } from '@/features/auth/components/AuthLayout'
import { PasswordInput } from '@/features/auth/components/PasswordInput'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Checkbox } from '@/components/ui/checkbox'
import { Skeleton } from '@/components/ui/skeleton'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { OptionSelect } from '@/components/ops/filter-select'
import { Stepper } from '@/components/ops/stepper'
import { useOneTimeToken } from '@/components/ops/use-one-time-token'
import { emptyToNull } from '@/components/ops/utils'
import { errorMessage, isApiError } from '@/lib/errors'
import { tokenStore } from '@/lib/http'
import { qk } from '@/lib/queryKeys'
import { formatDateTime } from '@/lib/format'
import { phoneProblem } from '@/lib/phone'
import { useAuthStore } from '@/stores/authStore'
import { UFS } from '@/pages/ops/api/labels'
import { docError } from '@/pages/ops/onboarding/stepRules'
import { advertiserSignupApi, type SignupCompleteResult, type SignupOptions, type SignupTerms } from './api'
import { EMAIL_RE, signupPasswordProblems, stepForError, type SignupStep } from './rules'

const KEY = qk.sub('organizations', 'advertiser-signup')
const STEPS: Array<{ key: SignupStep; label: string }> = [
  { key: 'empresa', label: 'Empresa e contato' },
  { key: 'segmentos', label: 'Segmentos' },
  { key: 'acesso', label: 'Seu acesso' },
  { key: 'termos', label: 'Termos e privacidade' },
  { key: 'revisao', label: 'Revisão' },
]

interface FormState {
  docType: 'CNPJ' | 'CPF'
  document: string
  name: string
  tradeName: string
  responsible: string
  phone: string
  contactEmail: string
  postal: string
  address: string
  number: string
  complement: string
  neighborhood: string
  city: string
  state: string | null
  categoryId: string | null
  segmentIds: string[]
  fullName: string
  password: string
  confirm: string
  acceptTerms: boolean
  acceptPrivacy: boolean
  marketing: boolean
}

const INITIAL: FormState = {
  docType: 'CNPJ',
  document: '',
  name: '',
  tradeName: '',
  responsible: '',
  phone: '',
  contactEmail: '',
  postal: '',
  address: '',
  number: '',
  complement: '',
  neighborhood: '',
  city: '',
  state: null,
  categoryId: null,
  segmentIds: [],
  fullName: '',
  password: '',
  confirm: '',
  acceptTerms: false,
  acceptPrivacy: false,
  marketing: false,
}

type Errors = Partial<Record<keyof FormState, string>>

function validate(step: SignupStep, f: FormState, email: string, terms: SignupTerms | undefined): Errors {
  const e: Errors = {}
  if (step === 'empresa') {
    if (f.name.trim().length < 2) e.name = f.docType === 'CPF' ? 'Informe seu nome completo.' : 'Informe a razão social.'
    const d = docError(f.document, f.docType, true)
    if (d) e.document = d
    // mesmo critério do pagamento (DDD + número) — AÇÃO 004
    const phone = phoneProblem(f.phone)
    if (phone) e.phone = phone
    if (f.contactEmail.trim() && !EMAIL_RE.test(f.contactEmail.trim())) e.contactEmail = 'E-mail inválido.'
    if (f.postal.trim() && f.postal.replace(/\D/g, '').length !== 8) e.postal = 'CEP com 8 dígitos.'
    if (f.city.trim().length < 2) e.city = 'Informe a cidade.'
    if (!f.state) e.state = 'Selecione a UF.'
  }
  if (step === 'segmentos') {
    if (!f.segmentIds.length) e.segmentIds = 'Escolha pelo menos um segmento do seu negócio.'
    if (f.segmentIds.length > 50) e.segmentIds = 'Escolha no máximo 50 segmentos.'
  }
  if (step === 'acesso') {
    if (f.fullName.trim().length < 2) e.fullName = 'Informe seu nome.'
    if (terms) {
      const p = signupPasswordProblems(f.password, email, terms.password_policy)
      if (p.length) e.password = p.join(' ')
    }
    if (f.confirm !== f.password) e.confirm = 'As senhas não coincidem.'
  }
  if (step === 'termos') {
    if (!f.acceptTerms) e.acceptTerms = 'É preciso aceitar os Termos do Anunciante.'
    if (!f.acceptPrivacy) e.acceptPrivacy = 'É preciso aceitar os Termos de uso e a Política de privacidade.'
  }
  return e
}

// --------------------------------------------------------------------------- e-mail inicial
function StartForm({ captchaRequired }: { captchaRequired: boolean }) {
  const [email, setEmail] = React.useState('')
  const [touched, setTouched] = React.useState(false)
  const emailRef = React.useRef<HTMLInputElement>(null)
  const valid = EMAIL_RE.test(email.trim())
  const m = useMutation({ mutationFn: () => advertiserSignupApi.start(email.trim().toLowerCase()) })
  if (m.isSuccess)
    return (
      <Alert variant="success">
        <MailCheck aria-hidden="true" />
        <div>
          <AlertTitle>Confira seu e-mail</AlertTitle>
          <AlertDescription>
            {m.data.message} O link vale por 24 horas e só pode ser usado uma vez. Confira também a caixa de spam.
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
        else emailRef.current?.focus()
      }}
    >
      <ol className="m-0 flex list-decimal flex-col gap-1 pl-5 text-sm text-text-muted">
        <li>Informe seu e-mail.</li>
        <li>Confirme o endereço pelo link que enviaremos.</li>
        <li>Preencha os dados da empresa e aceite os termos.</li>
        <li>Crie sua campanha ou fale com o nosso comercial.</li>
      </ol>
      {captchaRequired ? (
        <Alert variant="warning">
          <AlertDescription className="text-text">
            Este ambiente exige verificação anti-robô, que ainda não está disponível nesta página. Se o envio for recusado, fale
            com o comercial da RD.
          </AlertDescription>
        </Alert>
      ) : null}
      <Field invalid={touched && !valid} required>
        <FieldLabel>E-mail</FieldLabel>
        <Input ref={emailRef} type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" />
        <FieldDescription>Será o seu login no painel.</FieldDescription>
        <FieldError>{touched && !valid ? 'Informe um e-mail válido.' : null}</FieldError>
      </Field>
      {m.error ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">
            {captcha ? 'A verificação anti-robô não pôde ser concluída. Tente mais tarde ou fale com o comercial.' : errorMessage(m.error)}
          </AlertDescription>
        </Alert>
      ) : null}
      <Button type="submit" size="block" loading={m.isPending}>
        Enviar link de cadastro
      </Button>
    </form>
  )
}

// --------------------------------------------------------------------------- concluído
function Done({ result }: { result: SignupCompleteResult }) {
  const navigate = useNavigate()
  const [askSales, setAskSales] = React.useState(false)
  const [message, setMessage] = React.useState('')
  const sales = useMutation({ mutationFn: () => advertiserSignupApi.salesContact(result.organization.id, emptyToNull(message)) })
  return (
    <div className="flex flex-col gap-6">
      <Alert variant="success">
        <CheckCircle2 aria-hidden="true" />
        <div>
          <AlertTitle>Conta de anunciante criada</AlertTitle>
          <AlertDescription>
            {result.organization.name} está pronta para anunciar. Termos do Anunciante aceitos na versão {result.terms_acceptance.version}.
          </AlertDescription>
        </div>
      </Alert>
      <section aria-labelledby="proximo-passo" className="flex flex-col gap-3">
        <h2 id="proximo-passo" className="m-0 text-title-section text-text">
          Como você quer continuar?
        </h2>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Button size="block" onClick={() => navigate('/anunciante/campanhas/nova')}>
            <Megaphone aria-hidden="true" />
            Criar minha campanha agora
          </Button>
          <Button size="block" variant="secondary" onClick={() => setAskSales(true)} aria-expanded={askSales}>
            <Headset aria-hidden="true" />
            Falar com o comercial
          </Button>
        </div>
      </section>
      {askSales ? (
        sales.isSuccess ? (
          <Alert variant="success">
            <AlertDescription className="text-text">
              {sales.data.already_requested ? 'Seu pedido de contato já estava registrado. ' : ''}
              {sales.data.message}
            </AlertDescription>
          </Alert>
        ) : (
          <form
            noValidate
            className="flex flex-col gap-3"
            onSubmit={(e) => {
              e.preventDefault()
              sales.mutate()
            }}
          >
            <Field>
              <FieldLabel>Mensagem para o comercial (opcional)</FieldLabel>
              <Textarea value={message} onChange={(e) => setMessage(e.target.value)} rows={3} maxLength={1000} />
              <FieldDescription>Conte o que você quer anunciar, região e período desejados.</FieldDescription>
            </Field>
            {sales.error ? (
              <Alert variant="danger">
                <AlertDescription className="text-text">{errorMessage(sales.error)}</AlertDescription>
              </Alert>
            ) : null}
            <Button type="submit" variant="secondary" className="w-fit" loading={sales.isPending}>
              <Send aria-hidden="true" />
              Pedir contato do comercial
            </Button>
          </form>
        )
      ) : null}
      <Link to="/anunciante" className="inline-flex min-h-11 w-fit items-center text-sm font-semibold text-link underline-offset-4 hover:underline">
        Ir para o painel do anunciante
      </Link>
    </div>
  )
}

// --------------------------------------------------------------------------- formulário (com token)
function SignupWizard({
  token,
  email,
  expiresAt,
  terms,
  options,
  onDone,
  onInvalidLink,
  refetchTerms,
}: {
  token: string
  email: string
  expiresAt: string
  terms: SignupTerms
  options: SignupOptions
  onDone: (r: SignupCompleteResult) => void
  onInvalidLink: () => void
  refetchTerms: () => void
}) {
  const [step, setStep] = React.useState<SignupStep>('empresa')
  const [f, setF] = React.useState<FormState>(INITIAL)
  const [errors, setErrors] = React.useState<Errors>({})
  const [serverError, setServerError] = React.useState<string | null>(null)
  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => setF((s) => ({ ...s, [k]: v }))
  const termsPublished = !!terms.advertiser_terms
  const sectionRef = React.useRef<HTMLElement>(null)
  const headingRef = React.useRef<HTMLHeadingElement>(null)
  const firstRender = React.useRef(true)
  // troca de etapa: o foco vai para o título da nova etapa (leitores de tela anunciam a etapa)
  React.useEffect(() => {
    if (firstRender.current) {
      firstRender.current = false
      return
    }
    headingRef.current?.focus()
  }, [step])

  const goNext = () => {
    const e = validate(step, f, email, terms)
    setErrors(e)
    if (Object.keys(e).length) {
      // erro de validação: foco no primeiro campo inválido da etapa
      requestAnimationFrame(() => sectionRef.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus())
      return
    }
    const i = STEPS.findIndex((s) => s.key === step)
    setStep(STEPS[Math.min(i + 1, STEPS.length - 1)].key)
  }

  const complete = useMutation({
    mutationFn: () =>
      advertiserSignupApi.complete(token, {
        organization: {
          name: f.name.trim(),
          trade_name: emptyToNull(f.tradeName),
          document: f.document.trim(),
          document_type: f.docType,
          responsible_name: emptyToNull(f.responsible),
          phone: f.phone.trim(),
          contact_email: emptyToNull(f.contactEmail),
          address: emptyToNull(f.address),
          number: emptyToNull(f.number),
          complement: emptyToNull(f.complement),
          neighborhood: emptyToNull(f.neighborhood),
          city: f.city.trim(),
          state: f.state!,
          postal_code: emptyToNull(f.postal),
          category_id: f.categoryId,
          segment_ids: f.segmentIds,
        },
        user: { full_name: f.fullName.trim(), password: f.password },
        consents: {
          terms_template_id: terms.advertiser_terms!.id,
          accept_terms: f.acceptTerms,
          terms_version: terms.terms_version,
          privacy_version: terms.privacy_version,
          accept_privacy: f.acceptPrivacy,
          marketing_opt_in: f.marketing,
        },
        contact_sales: false,
        sales_message: null,
      }),
    onSuccess: async (r) => {
      // adota a sessão devolvida (o cookie de refresh já foi gravado pelo navegador)
      tokenStore.set(r.access_token)
      try {
        await useAuthStore.getState().reloadMe()
      } catch {
        /* sem /me: o usuário ainda pode entrar pelo login */
      }
      setF((s) => ({ ...s, password: '', confirm: '' }))
      onDone(r)
    },
    onError: (e) => {
      if (!isApiError(e)) return setServerError(errorMessage(e))
      if (e.code === 'SIGNUP_TOKEN_INVALID') return onInvalidLink()
      const loc = Object.keys(e.fieldErrors)[0]
      const target = stepForError(e.code, loc)
      if (e.code === 'PASSWORD_POLICY') {
        const list = Array.isArray(e.details.errors) ? (e.details.errors as string[]).join(' ') : e.message
        setErrors({ password: list })
      } else if (e.code === 'DOCUMENT_INVALID' || e.code === 'DOCUMENT_ALREADY_REGISTERED') {
        setErrors({ document: e.message })
      } else if (e.code === 'SEGMENT_NOT_FOUND') {
        setErrors({ segmentIds: e.message })
      } else if (e.code === 'CATEGORY_NOT_FOUND' || e.code === 'CATEGORY_TOO_BROAD') {
        setErrors({ categoryId: e.message })
      }
      if (['CONSENT_VERSION_OUTDATED', 'TERMS_VERSION_OUTDATED', 'TERMS_NOT_PUBLISHED'].includes(e.code)) {
        refetchTerms()
        setF((s) => ({ ...s, acceptTerms: false, acceptPrivacy: false }))
      }
      setServerError(e.message)
      setStep(target)
    },
  })

  const submit = () => {
    setServerError(null)
    for (const s of STEPS) {
      const e = validate(s.key, f, email, terms)
      if (Object.keys(e).length) {
        setErrors(e)
        setStep(s.key)
        return
      }
    }
    if (!termsPublished) return
    complete.mutate()
  }

  const done = (k: string) => Object.keys(validate(k as SignupStep, f, email, terms)).length === 0 && k !== 'revisao'
  const selectableCategories = options.categories.filter((c) => c.kind !== 'SECTOR')

  let body: React.ReactNode
  if (step === 'empresa') {
    body = (
      <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
        <div className="flex flex-col gap-2 md:col-span-2">
          <span id="adv-doc-type" className="text-sm font-semibold text-text">
            Você anuncia como
          </span>
          <ToggleGroup type="single" value={f.docType} onValueChange={(v) => v && set('docType', v as 'CPF' | 'CNPJ')} aria-labelledby="adv-doc-type">
            <ToggleGroupItem value="CNPJ">Empresa (CNPJ)</ToggleGroupItem>
            <ToggleGroupItem value="CPF">Pessoa física (CPF)</ToggleGroupItem>
          </ToggleGroup>
        </div>
        <Field invalid={!!errors.document} required>
          <FieldLabel>{f.docType}</FieldLabel>
          <Input value={f.document} onChange={(e) => set('document', e.target.value)} autoComplete="off" />
          <FieldDescription>{f.docType === 'CNPJ' ? 'Com ou sem máscara; CNPJ alfanumérico aceito.' : 'Com ou sem máscara.'}</FieldDescription>
          <FieldError>{errors.document}</FieldError>
        </Field>
        <Field invalid={!!errors.name} required>
          <FieldLabel>{f.docType === 'CNPJ' ? 'Razão social' : 'Nome completo'}</FieldLabel>
          <Input value={f.name} onChange={(e) => set('name', e.target.value)} autoComplete={f.docType === 'CNPJ' ? 'organization' : 'name'} />
          <FieldError>{errors.name}</FieldError>
        </Field>
        {f.docType === 'CNPJ' ? (
          <Field>
            <FieldLabel>Nome fantasia</FieldLabel>
            <Input value={f.tradeName} onChange={(e) => set('tradeName', e.target.value)} />
          </Field>
        ) : null}
        <Field>
          <FieldLabel>Responsável</FieldLabel>
          <Input value={f.responsible} onChange={(e) => set('responsible', e.target.value)} autoComplete="name" />
        </Field>
        <Field invalid={!!errors.phone} required>
          <FieldLabel>Telefone</FieldLabel>
          <Input type="tel" value={f.phone} onChange={(e) => set('phone', e.target.value)} autoComplete="tel" />
          <FieldError>{errors.phone}</FieldError>
        </Field>
        <Field invalid={!!errors.contactEmail}>
          <FieldLabel>E-mail de contato da empresa</FieldLabel>
          <Input type="email" value={f.contactEmail} onChange={(e) => set('contactEmail', e.target.value)} autoComplete="email" />
          <FieldDescription>Opcional, se for diferente do seu login ({email}).</FieldDescription>
          <FieldError>{errors.contactEmail}</FieldError>
        </Field>
        <Field invalid={!!errors.postal}>
          <FieldLabel>CEP</FieldLabel>
          <Input value={f.postal} onChange={(e) => set('postal', e.target.value)} inputMode="numeric" autoComplete="postal-code" />
          <FieldError>{errors.postal}</FieldError>
        </Field>
        <Field>
          <FieldLabel>Logradouro</FieldLabel>
          <Input value={f.address} onChange={(e) => set('address', e.target.value)} autoComplete="address-line1" />
        </Field>
        <Field>
          <FieldLabel>Número</FieldLabel>
          <Input value={f.number} onChange={(e) => set('number', e.target.value)} />
        </Field>
        <Field>
          <FieldLabel>Complemento</FieldLabel>
          <Input value={f.complement} onChange={(e) => set('complement', e.target.value)} autoComplete="address-line2" />
        </Field>
        <Field>
          <FieldLabel>Bairro</FieldLabel>
          <Input value={f.neighborhood} onChange={(e) => set('neighborhood', e.target.value)} />
        </Field>
        <Field invalid={!!errors.state} required>
          <FieldLabel>UF</FieldLabel>
          <OptionSelect value={f.state} onChange={(v) => set('state', v)} placeholder="Selecione a UF" options={UFS.map((u) => ({ value: u.uf, label: `${u.uf} — ${u.name}` }))} />
          <FieldError>{errors.state}</FieldError>
        </Field>
        <Field invalid={!!errors.city} required>
          <FieldLabel>Cidade</FieldLabel>
          <Input value={f.city} onChange={(e) => set('city', e.target.value)} autoComplete="address-level2" />
          <FieldError>{errors.city}</FieldError>
        </Field>
      </div>
    )
  } else if (step === 'segmentos') {
    body = (
      <div className="flex flex-col gap-5">
        <Field invalid={!!errors.categoryId} className="max-w-md">
          <FieldLabel>Categoria do seu negócio (opcional)</FieldLabel>
          <OptionSelect
            value={f.categoryId}
            onChange={(v) => set('categoryId', v)}
            includeNone
            noneLabel="Não informar"
            options={selectableCategories.map((c) => ({ value: c.id, label: `${'— '.repeat(Math.max(0, c.depth - 1))}${c.name}` }))}
          />
          <FieldError>{errors.categoryId}</FieldError>
        </Field>
        <fieldset className="m-0 flex flex-col border-0 p-0" aria-describedby="adv-seg-help">
          <legend className="mb-1 text-sm font-semibold text-text">
            Segmentos que você anuncia<span className="ml-1 text-text-muted" aria-hidden="true">*</span>
            <span className="sr-only"> (obrigatório)</span>
          </legend>
          <p id="adv-seg-help" className="m-0 mb-2 text-xs text-text-muted">
            Usados para evitar anúncios de concorrentes no mesmo bloco e respeitar as regras de conteúdo.
          </p>
          <div className="grid max-h-72 grid-cols-1 gap-0 overflow-y-auto sm:grid-cols-2">
            {options.segments.map((s) => (
              <Field key={s.id} orientation="horizontal" className="min-h-11" style={{ paddingLeft: `${s.depth * 16}px` }}>
                <Checkbox
                  checked={f.segmentIds.includes(s.id)}
                  onCheckedChange={(c) => set('segmentIds', c ? [...f.segmentIds, s.id] : f.segmentIds.filter((x) => x !== s.id))}
                />
                <FieldLabel className="font-normal">{s.name}</FieldLabel>
              </Field>
            ))}
          </div>
          {!options.segments.length ? <p className="m-0 text-sm text-text-muted">Nenhum segmento disponível no momento.</p> : null}
          {errors.segmentIds ? (
            <p className="m-0 mt-2 text-xs font-medium text-danger" role="alert">
              {errors.segmentIds}
            </p>
          ) : null}
        </fieldset>
      </div>
    )
  } else if (step === 'acesso') {
    const p = terms.password_policy
    body = (
      <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
        <Field className="md:col-span-2">
          <FieldLabel>Seu login</FieldLabel>
          <Input value={email} readOnly aria-readonly="true" />
          <FieldDescription>E-mail confirmado pelo link. O link vale até {formatDateTime(expiresAt)}.</FieldDescription>
        </Field>
        <Field invalid={!!errors.fullName} required className="md:col-span-2">
          <FieldLabel>Seu nome</FieldLabel>
          <Input value={f.fullName} onChange={(e) => set('fullName', e.target.value)} autoComplete="name" />
          <FieldError>{errors.fullName}</FieldError>
        </Field>
        <Field invalid={!!errors.password} required>
          <FieldLabel>Senha</FieldLabel>
          <PasswordInput value={f.password} onChange={(e) => set('password', e.target.value)} autoComplete="new-password" />
          <FieldDescription>
            De {p.min_length} a {p.max_length} caracteres{p.requires_letters_and_digits ? ', com letras e números' : ''}
            {p.must_not_contain_email ? ', sem conter seu e-mail' : ''}.
          </FieldDescription>
          <FieldError>{errors.password}</FieldError>
        </Field>
        <Field invalid={!!errors.confirm} required>
          <FieldLabel>Confirme a senha</FieldLabel>
          <PasswordInput value={f.confirm} onChange={(e) => set('confirm', e.target.value)} autoComplete="new-password" />
          <FieldError>{errors.confirm}</FieldError>
        </Field>
      </div>
    )
  } else if (step === 'termos') {
    const t = terms.advertiser_terms
    body = (
      <div className="flex flex-col gap-5">
        {t ? (
          <>
            <p className="m-0 text-sm text-text-muted">
              {t.title} · versão {t.version}
            </p>
            <div
              tabIndex={0}
              role="document"
              aria-label="Texto dos Termos do Anunciante"
              className="max-h-[40vh] overflow-y-auto whitespace-pre-wrap rounded-lg border border-border bg-field p-4 text-sm leading-relaxed text-text"
            >
              {t.body}
            </div>
            <Field orientation="horizontal" className="min-h-11" invalid={!!errors.acceptTerms}>
              <Checkbox checked={f.acceptTerms} onCheckedChange={(c) => set('acceptTerms', !!c)} />
              <FieldLabel className="font-normal">Li e aceito os Termos do Anunciante (versão {t.version}).</FieldLabel>
              <FieldError className="w-full">{errors.acceptTerms}</FieldError>
            </Field>
          </>
        ) : (
          <Alert variant="warning">
            <AlertTitle>Termos do Anunciante ainda não publicados</AlertTitle>
            <AlertDescription>
              O cadastro só pode ser concluído depois que a RD publicar a versão vigente dos termos. Seus dados não são enviados
              até lá; tente novamente mais tarde ou fale com o comercial.
            </AlertDescription>
          </Alert>
        )}
        <Field orientation="horizontal" className="min-h-11" invalid={!!errors.acceptPrivacy}>
          <Checkbox checked={f.acceptPrivacy} onCheckedChange={(c) => set('acceptPrivacy', !!c)} />
          <FieldLabel className="font-normal">
            Aceito os Termos de uso (versão {terms.terms_version}) e a Política de privacidade (versão {terms.privacy_version}).
          </FieldLabel>
          <FieldError className="w-full">{errors.acceptPrivacy}</FieldError>
        </Field>
        <Field orientation="horizontal" className="min-h-11">
          <Checkbox checked={f.marketing} onCheckedChange={(c) => set('marketing', !!c)} />
          <FieldLabel className="font-normal">Quero receber novidades e ofertas da RD Mídia Indoor (opcional).</FieldLabel>
        </Field>
        <p className="m-0 text-xs text-text-muted">
          Os aceites ficam registrados com a versão, a data, a hora e o IP; você pode rever seus consentimentos em Minha conta.
        </p>
      </div>
    )
  } else {
    const segNames = options.segments.filter((s) => f.segmentIds.includes(s.id)).map((s) => s.name)
    body = (
      <div className="flex flex-col gap-4 text-sm">
        <dl className="m-0 grid grid-cols-1 gap-x-6 gap-y-3 sm:grid-cols-2">
          <div>
            <dt className="text-xs text-text-muted">{f.docType === 'CNPJ' ? 'Razão social' : 'Nome'}</dt>
            <dd className="m-0 text-text">{f.name || '—'}</dd>
          </div>
          <div>
            <dt className="text-xs text-text-muted">{f.docType}</dt>
            <dd className="m-0 text-text">{f.document || '—'}</dd>
          </div>
          <div>
            <dt className="text-xs text-text-muted">Cidade/UF</dt>
            <dd className="m-0 text-text">{f.city ? `${f.city}/${f.state ?? ''}` : '—'}</dd>
          </div>
          <div>
            <dt className="text-xs text-text-muted">Login</dt>
            <dd className="m-0 text-text">{email}</dd>
          </div>
          <div className="sm:col-span-2">
            <dt className="text-xs text-text-muted">Segmentos</dt>
            <dd className="m-0 text-text">{segNames.length ? segNames.join(', ') : '—'}</dd>
          </div>
        </dl>
        {!termsPublished ? (
          <Alert variant="warning">
            <AlertDescription className="text-text">Conclusão indisponível: os Termos do Anunciante ainda não foram publicados.</AlertDescription>
          </Alert>
        ) : null}
        <Button size="block" onClick={submit} loading={complete.isPending} loadingText="Criando sua conta…" disabled={!termsPublished}>
          Criar conta de anunciante
        </Button>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-6">
      <Stepper label="Etapas do cadastro de anunciante" steps={STEPS} current={step} onSelect={(k) => setStep(k as SignupStep)} completed={done} />
      {serverError ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">{serverError}</AlertDescription>
        </Alert>
      ) : null}
      <section ref={sectionRef} aria-labelledby="adv-etapa" className="flex flex-col gap-5">
        <h2 ref={headingRef} id="adv-etapa" tabIndex={-1} className="m-0 text-title-section text-text outline-none">
          {STEPS.find((s) => s.key === step)?.label}
        </h2>
        {body}
        <div className="flex flex-col-reverse gap-3 sm:flex-row sm:justify-between">
          <Button
            variant="secondary"
            disabled={step === 'empresa' || complete.isPending}
            onClick={() => setStep(STEPS[Math.max(0, STEPS.findIndex((s) => s.key === step) - 1)].key)}
          >
            Voltar
          </Button>
          {step !== 'revisao' ? <Button onClick={goNext}>Continuar</Button> : null}
        </div>
      </section>
    </div>
  )
}

export default function AdvertiserSignupPage() {
  const token = useOneTimeToken()
  const [linkInvalid, setLinkInvalid] = React.useState(false)
  const [result, setResult] = React.useState<SignupCompleteResult | null>(null)
  const terms = useQuery({ queryKey: qk.sub('organizations', 'advertiser-signup', 'terms'), queryFn: advertiserSignupApi.terms, retry: false, staleTime: Infinity })
  const options = useQuery({
    queryKey: qk.sub('organizations', 'advertiser-signup', 'options'),
    queryFn: advertiserSignupApi.options,
    enabled: !!token,
    retry: false,
    staleTime: Infinity,
  })
  const app = useQuery({
    queryKey: [...KEY, 'application'],
    queryFn: () => advertiserSignupApi.application(token!),
    enabled: !!token && !linkInvalid,
    retry: false,
    staleTime: Infinity,
  })
  const invalid = linkInvalid || (isApiError(app.error) && app.error.status === 401)

  let content: React.ReactNode
  if (result) content = <Done result={result} />
  else if (!token)
    content = terms.isLoading ? <Skeleton className="h-40 w-full" /> : <StartForm captchaRequired={!!terms.data?.captcha_required} />
  else if (invalid)
    content = (
      <div className="flex flex-col gap-5">
        <Alert variant="warning">
          <AlertTitle>Link inválido, expirado ou já utilizado</AlertTitle>
          <AlertDescription>O link vale por 24 horas e uma única vez. Informe seu e-mail para receber um novo.</AlertDescription>
        </Alert>
        <StartForm captchaRequired={!!terms.data?.captcha_required} />
      </div>
    )
  else if (app.isLoading || terms.isLoading || options.isLoading)
    content = (
      <div role="status" className="flex flex-col gap-3">
        <span className="sr-only">Carregando seu cadastro…</span>
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-48 w-full" />
      </div>
    )
  else if (app.error || terms.error || options.error || !app.data || !terms.data || !options.data)
    content = (
      <Alert variant="danger">
        <AlertDescription className="text-text">{errorMessage(app.error ?? terms.error ?? options.error)}</AlertDescription>
      </Alert>
    )
  else
    content = (
      <SignupWizard
        token={token}
        email={app.data.email}
        expiresAt={app.data.expires_at}
        terms={terms.data}
        options={options.data}
        onDone={setResult}
        onInvalidLink={() => setLinkInvalid(true)}
        refetchTerms={() => void terms.refetch()}
      />
    )

  return (
    <AuthLayout
      wide
      title={result ? 'Tudo pronto!' : 'Crie sua conta de anunciante'}
      description={result ? undefined : 'Anuncie nas TVs dos estabelecimentos parceiros da RD Mídia Indoor.'}
      footer={
        result ? null : (
          <Link to="/login" className="text-link underline-offset-4 hover:underline">
            Já tenho conta — entrar
          </Link>
        )
      }
    >
      {content}
    </AuthLayout>
  )
}
