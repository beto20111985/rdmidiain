/**
 * Ativação do 2º Administrador Master — `/ativar-master#token=…` (pública; SPEC AÇÃO 003 §11.6).
 *  - O token vem do FRAGMENTO: é lido e REMOVIDO da barra de endereço na hora e fica só em memória
 *    (ref). Nada em localStorage/sessionStorage, query string ou queryKey; só vai no corpo dos POSTs.
 *  - Quem ativa cria a PRÓPRIA senha, o PRÓPRIO TOTP e a PRÓPRIA senha financeira; ninguém (nem quem
 *    convidou) as conhece. Passos: Criar senha → Configurar TOTP → Criar senha financeira → Concluir.
 *  - Erros genéricos: a tela não revela se o convite não existe, expirou, foi revogado ou aceito.
 *  - Códigos de recuperação aparecem UMA vez, só em memória.
 */
import * as React from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { ShieldCheck } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Spinner } from '@/components/ui/spinner'
import { isApiError } from '@/lib/errors'
import { formatDateTime } from '@/lib/format'
import { useAuthStore } from '@/stores/authStore'
import { totpSchema } from '@/components/finance/step-up'
import { authApi } from '../api'
import { takeTokenFromFragment } from '../activationToken'
import { masterActivationErrorMessage } from '../authErrors'
import { newPasswordSchema } from '../passwordSchema'
import { AuthLayout } from '../components/AuthLayout'
import { NewPasswordFields } from '../components/NewPasswordFields'
import { PasswordInput } from '../components/PasswordInput'
import { QrCodeSvg } from '../components/QrCodeSvg'
import { RecoveryCodesPanel } from '../components/RecoveryCodesPanel'
import { StepProgress, type StepProgressItem } from '../components/StepProgress'
import type { MasterActivationInfo, TotpSetup } from '../types'

type Step = 'PASSWORD' | 'TOTP' | 'FINANCIAL_PASSWORD' | 'FINISH'
const STEPS: Array<{ code: Step; label: string }> = [
  { code: 'PASSWORD', label: 'Criar senha' },
  { code: 'TOTP', label: 'Configurar TOTP' },
  { code: 'FINANCIAL_PASSWORD', label: 'Criar senha financeira' },
  { code: 'FINISH', label: 'Concluir' },
]

function groupSecret(secret: string) {
  return secret.replace(/\s+/g, '').match(/.{1,4}/g)?.join(' ') ?? secret
}

// ---------------------------------------------------------------------------
const passwordSchema = newPasswordSchema({})
type PasswordValues = z.infer<typeof passwordSchema>

function PasswordStep({ initial, onNext }: { initial: string; onNext: (password: string) => void }) {
  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<PasswordValues>({ resolver: zodResolver(passwordSchema), defaultValues: { new_password: initial, confirm: initial } })
  return (
    <form onSubmit={handleSubmit((v) => onNext(v.new_password))} noValidate className="flex flex-col gap-6" aria-label="Criar senha">
      <FieldGroup>
        <NewPasswordFields register={register} errors={errors} label="Sua senha de acesso" />
      </FieldGroup>
      <Button type="submit" size="block">
        Continuar
      </Button>
    </form>
  )
}

// ---------------------------------------------------------------------------
function TotpStep({ totp, error, loading, onRetry, onBack, onNext }: {
  totp: TotpSetup | null
  error: string | null
  loading: boolean
  onRetry: () => void
  onBack: () => void
  onNext: () => void
}) {
  if (loading && !totp) {
    return (
      <div className="flex items-center gap-2 text-sm text-text-muted" role="status">
        <Spinner label="Gerando a chave do autenticador…" />
        <span aria-hidden="true">Gerando a chave do autenticador…</span>
      </div>
    )
  }
  if (!totp) {
    return (
      <div className="flex flex-col gap-4">
        <Alert variant="danger">
          <AlertDescription className="text-text">{error ?? 'Não foi possível gerar a chave do autenticador.'}</AlertDescription>
        </Alert>
        <div className="flex flex-wrap gap-3">
          <Button onClick={onRetry} loading={loading}>
            Tentar novamente
          </Button>
          <Button variant="secondary" onClick={onBack}>
            Voltar
          </Button>
        </div>
      </div>
    )
  }
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col items-center gap-4 sm:flex-row sm:items-start">
        <QrCodeSvg value={totp.otpauth_uri} label="QR code para cadastrar a conta no aplicativo autenticador" />
        <div className="flex flex-col gap-3 text-sm">
          <p className="m-0 text-text">
            No celular, abra o <strong>Google Authenticator</strong> (ou outro aplicativo TOTP) e leia o QR code.
          </p>
          <p className="m-0 text-text-muted">Sem câmera? Digite esta chave no aplicativo:</p>
          <code className="break-all rounded-md border border-border bg-field px-3 py-2 font-mono text-sm tracking-wider text-text">
            {groupSecret(totp.secret)}
          </code>
          <p className="m-0 text-text-muted">No último passo você digitará o código atual que aparece no aplicativo.</p>
        </div>
      </div>
      <div className="flex flex-wrap gap-3">
        <Button onClick={onNext}>Já cadastrei, continuar</Button>
        <Button variant="secondary" onClick={onBack}>
          Voltar
        </Button>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
function financialSchemaFor(loginPassword: string) {
  return z
    .object({
      financial_password: z.string().min(8, 'Use pelo menos 8 caracteres.').max(128, 'Senha longa demais.'),
      confirm: z.string(),
    })
    .refine((v) => v.financial_password === v.confirm, { path: ['confirm'], message: 'As senhas não conferem.' })
    .refine((v) => v.financial_password !== loginPassword, {
      path: ['financial_password'],
      message: 'A senha financeira deve ser diferente da senha de acesso.',
    })
}

function FinancialStep({ initial, loginPassword, serverError, onNext, onBack }: {
  initial: string
  loginPassword: string
  serverError: string | null
  onNext: (financial: string) => void
  onBack: () => void
}) {
  const schema = React.useMemo(() => financialSchemaFor(loginPassword), [loginPassword])
  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<z.infer<typeof schema>>({ resolver: zodResolver(schema), defaultValues: { financial_password: initial, confirm: initial } })
  return (
    <form onSubmit={handleSubmit((v) => onNext(v.financial_password))} noValidate className="flex flex-col gap-6" aria-label="Criar senha financeira">
      {serverError ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">{serverError}</AlertDescription>
        </Alert>
      ) : null}
      <FieldGroup>
        <Field invalid={!!errors.financial_password}>
          <FieldLabel required>Senha financeira</FieldLabel>
          <PasswordInput autoComplete="new-password" {...register('financial_password')} />
          <FieldDescription>
            Entre 8 e 128 caracteres, diferente da senha de acesso. Será exigida, junto com o código do autenticador, para autorizar operações
            financeiras. Só você a conhece.
          </FieldDescription>
          <FieldError>{errors.financial_password?.message}</FieldError>
        </Field>
        <Field invalid={!!errors.confirm}>
          <FieldLabel required>Confirme a senha financeira</FieldLabel>
          <PasswordInput autoComplete="new-password" {...register('confirm')} />
          <FieldError>{errors.confirm?.message}</FieldError>
        </Field>
      </FieldGroup>
      <div className="flex flex-wrap gap-3">
        <Button type="submit">Continuar</Button>
        <Button type="button" variant="secondary" onClick={onBack}>
          Voltar
        </Button>
      </div>
    </form>
  )
}

// ---------------------------------------------------------------------------
const finishSchema = z.object({ totp_code: totpSchema })

function FinishStep({ pending, error, onSubmit, onBack }: {
  pending: boolean
  error: string | null
  onSubmit: (code: string) => void
  onBack: () => void
}) {
  const {
    register,
    handleSubmit,
    resetField,
    formState: { errors },
  } = useForm<z.infer<typeof finishSchema>>({ resolver: zodResolver(finishSchema), defaultValues: { totp_code: '' } })
  return (
    <form
      onSubmit={handleSubmit((v) => {
        onSubmit(v.totp_code.trim())
        resetField('totp_code') // o código é de uso único
      })}
      noValidate
      className="flex flex-col gap-6"
      aria-label="Concluir a ativação"
    >
      <ul className="m-0 flex list-none flex-col gap-1 p-0 text-sm text-text">
        <li>Senha de acesso: definida</li>
        <li>Autenticador: chave cadastrada no aplicativo</li>
        <li>Senha financeira: definida</li>
      </ul>
      {error ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">{error}</AlertDescription>
        </Alert>
      ) : null}
      <Field invalid={!!errors.totp_code}>
        <FieldLabel required>Código atual do autenticador</FieldLabel>
        <Input inputMode="numeric" autoComplete="one-time-code" maxLength={6} className="max-w-[200px] text-center tracking-[0.3em] tabular-nums" autoFocus {...register('totp_code')} />
        <FieldDescription>Digite os 6 números que aparecem agora no aplicativo. Isso confirma que o autenticador está funcionando.</FieldDescription>
        <FieldError>{errors.totp_code?.message}</FieldError>
      </Field>
      <div className="flex flex-wrap gap-3">
        <Button type="submit" loading={pending} loadingText="Ativando…">
          <ShieldCheck aria-hidden="true" />
          Ativar conta de Administrador Master
        </Button>
        <Button type="button" variant="secondary" onClick={onBack} disabled={pending}>
          Voltar
        </Button>
      </div>
    </form>
  )
}

// ---------------------------------------------------------------------------
type Phase = 'checking' | 'invalid' | 'ready' | 'done'

export default function MasterActivationPage() {
  const navigate = useNavigate()
  const status = useAuthStore((s) => s.status)
  const me = useAuthStore((s) => s.me)
  const demo = useAuthStore((s) => s.demo)
  const adoptSession = useAuthStore((s) => s.adoptSession)
  // O token só existe aqui, em memória. `undefined` = ainda não lido; `null` = ausente.
  const tokenRef = React.useRef<string | null | undefined>(undefined)
  const [phase, setPhase] = React.useState<Phase>('checking')
  const [invalidMessage, setInvalidMessage] = React.useState('Este link não pode ser usado. Peça ao Administrador Master que convidou você para reenviar o convite.')
  const [info, setInfo] = React.useState<MasterActivationInfo | null>(null)
  const [step, setStep] = React.useState<Step>('PASSWORD')
  const [password, setPassword] = React.useState('')
  const [financial, setFinancial] = React.useState('')
  const [totp, setTotp] = React.useState<TotpSetup | null>(null)
  const [totpError, setTotpError] = React.useState<string | null>(null)
  const [totpLoading, setTotpLoading] = React.useState(false)
  const [financialError, setFinancialError] = React.useState<string | null>(null)
  const [finishError, setFinishError] = React.useState<string | null>(null)
  const [finishing, setFinishing] = React.useState(false)
  const [codes, setCodes] = React.useState<string[]>([])
  const headingRef = React.useRef<HTMLHeadingElement>(null)
  const lastStep = React.useRef<Step | null>(null)

  // Lê o fragmento UMA vez (efeitos em dobro do StrictMode reaproveitam o valor já lido) e consulta o convite.
  const inspected = React.useRef(false)
  React.useEffect(() => {
    if (inspected.current) return
    inspected.current = true
    tokenRef.current = takeTokenFromFragment()
    const token = tokenRef.current
    if (!token) {
      setInvalidMessage('O link está incompleto. Abra novamente o link recebido por e-mail, sem alterar o endereço.')
      setPhase('invalid')
      return
    }
    authApi.masterActivation
      .inspect(token)
      .then((res) => {
        setInfo(res)
        setPhase('ready')
      })
      .catch((e) => {
        if (isApiError(e) && e.isNetwork) setInvalidMessage('Não foi possível falar com o servidor. Verifique sua conexão e abra o link de novo.')
        else if (isApiError(e) && e.status !== 400) setInvalidMessage(masterActivationErrorMessage(e))
        setPhase('invalid')
      })
  }, [])

  const loadTotp = React.useCallback(async () => {
    const token = tokenRef.current
    if (!token) return
    setTotpLoading(true)
    setTotpError(null)
    try {
      setTotp(await authApi.masterActivation.totp(token))
    } catch (e) {
      if (isApiError(e) && e.code === 'MASTER_INVITE_INVALID') {
        setInvalidMessage(masterActivationErrorMessage(e))
        setPhase('invalid')
      } else setTotpError(masterActivationErrorMessage(e))
    } finally {
      setTotpLoading(false)
    }
  }, [])

  // A chave do autenticador é gerada UMA vez (cada chamada substitui a anterior no servidor).
  React.useEffect(() => {
    if (phase === 'ready' && step === 'TOTP' && !totp && !totpLoading && !totpError) void loadTotp()
  }, [phase, step, totp, totpLoading, totpError, loadTotp])

  React.useEffect(() => {
    if (lastStep.current && lastStep.current !== step) headingRef.current?.focus()
    lastStep.current = step
  }, [step])

  const complete = async (code: string) => {
    const token = tokenRef.current
    if (!token) return
    setFinishing(true)
    setFinishError(null)
    try {
      const res = await authApi.masterActivation.complete({ token, password, totp_code: code, financial_password: financial })
      setCodes(res.recovery_codes ?? [])
      // Segredos digitados saem da memória; o token deixa de existir.
      setPassword('')
      setFinancial('')
      tokenRef.current = null
      setPhase('done')
      void adoptSession(res.access_token).catch(() => undefined)
    } catch (e) {
      if (isApiError(e)) {
        if (e.code === 'MASTER_INVITE_INVALID' || e.code === 'MASTER_INVITE_LOCKED') {
          setInvalidMessage(masterActivationErrorMessage(e))
          setPhase('invalid')
          return
        }
        if (e.code === 'MFA_SETUP_NOT_STARTED') {
          // O servidor não tem chave pendente para este convite: volta ao passo do TOTP e gera uma nova.
          setTotp(null)
          setTotpError(null)
          setFinishError(masterActivationErrorMessage(e))
          setStep('TOTP')
          return
        }
        if (e.code === 'PASSWORD_POLICY') {
          setStep('PASSWORD')
          setFinishError(masterActivationErrorMessage(e))
          return
        }
        if (e.code === 'FINANCIAL_PASSWORD_POLICY') {
          setFinancialError(masterActivationErrorMessage(e))
          setStep('FINANCIAL_PASSWORD')
          return
        }
      }
      setFinishError(masterActivationErrorMessage(e))
    } finally {
      setFinishing(false)
    }
  }

  if (phase === 'checking') {
    return (
      <AuthLayout title="Ativar conta de Administrador Master" description="Verificando o seu convite…">
        <div className="flex justify-center py-6">
          <Spinner label="Verificando o convite…" />
        </div>
      </AuthLayout>
    )
  }

  if (phase === 'invalid') {
    return (
      <AuthLayout
        title="Convite inválido ou expirado"
        description="Não foi possível usar este link de ativação."
        footer={
          <Button variant="tertiary" asChild>
            <Link to="/login">Ir para o login</Link>
          </Button>
        }
      >
        <Alert variant="danger">
          <AlertDescription className="text-text">{invalidMessage}</AlertDescription>
        </Alert>
      </AuthLayout>
    )
  }

  if (phase === 'done') {
    return (
      <AuthLayout wide title="Conta ativada" description="Você agora é o segundo Administrador Master desta instalação.">
        <RecoveryCodesPanel codes={codes} confirmLabel="Ir para o painel" onConfirm={() => navigate('/', { replace: true })} />
      </AuthLayout>
    )
  }

  const index = STEPS.findIndex((s) => s.code === step)
  const progress: StepProgressItem[] = STEPS.map((s, i) => ({
    key: s.code,
    label: s.label,
    state: i < index ? 'done' : i === index ? 'current' : 'pending',
  }))
  const foreignSession = status === 'authenticated' && !demo && me

  return (
    <AuthLayout
      wide
      title="Ativar conta de Administrador Master"
      description={
        info
          ? `Olá, ${info.full_name}. Você foi convidado(a) para ${info.instance_name} (${info.email_masked}). Ninguém além de você conhecerá sua senha, seu autenticador e sua senha financeira.`
          : undefined
      }
    >
      {foreignSession ? (
        <Alert variant="warning">
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <AlertTitle>Este navegador já tem uma sessão ativa</AlertTitle>
            <AlertDescription className="text-text">
              Ao concluir a ativação, a sessão de {me.user.name} neste navegador será substituída pela nova conta.
            </AlertDescription>
          </div>
        </Alert>
      ) : null}
      {info ? (
        <p className="m-0 text-xs text-text-muted">
          Convite válido até <time dateTime={info.expires_at}>{formatDateTime(info.expires_at)}</time>.
        </p>
      ) : null}
      <StepProgress steps={progress} label="Progresso da ativação" />
      <section aria-labelledby="activation-step-title" className="flex flex-col gap-6">
        <h2 id="activation-step-title" ref={headingRef} tabIndex={-1} className="m-0 text-title-section text-text outline-none">
          {index + 1}. {STEPS[index].label}
        </h2>
        {finishError && step !== 'FINISH' ? (
          <Alert variant="danger">
            <AlertDescription className="text-text">{finishError}</AlertDescription>
          </Alert>
        ) : null}
        {step === 'PASSWORD' ? (
          <PasswordStep
            initial={password}
            onNext={(pw) => {
              setPassword(pw)
              setFinishError(null)
              setStep('TOTP')
            }}
          />
        ) : null}
        {step === 'TOTP' ? (
          <TotpStep
            totp={totp}
            error={totpError}
            loading={totpLoading}
            onRetry={() => void loadTotp()}
            onBack={() => setStep('PASSWORD')}
            onNext={() => setStep('FINANCIAL_PASSWORD')}
          />
        ) : null}
        {step === 'FINANCIAL_PASSWORD' ? (
          <FinancialStep
            initial={financial}
            loginPassword={password}
            serverError={financialError}
            onBack={() => setStep('TOTP')}
            onNext={(f) => {
              setFinancial(f)
              setFinancialError(null)
              setStep('FINISH')
            }}
          />
        ) : null}
        {step === 'FINISH' ? (
          <FinishStep pending={finishing} error={finishError} onSubmit={(code) => void complete(code)} onBack={() => setStep('FINANCIAL_PASSWORD')} />
        ) : null}
      </section>
    </AuthLayout>
  )
}
