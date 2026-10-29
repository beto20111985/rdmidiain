/**
 * Login (E2 #62–63): e-mail OU telefone + senha. Fluxos do contrato v1 §2:
 * ok → painel · mfa_required → /login/verificacao · setup_required → /primeiro-acesso.
 * Erros 401/423/429 ficam visíveis na tela (sem redirecionamento que apague a mensagem).
 */
import * as React from 'react'
import { Link, Navigate, useNavigate, useSearchParams } from 'react-router-dom'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { LogIn } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Field, FieldError, FieldGroup, FieldLabel, FieldDescription } from '@/components/ui/field'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { BootScreen } from '@/components/auth/boot-screen'
import { useAuthStore, type AuthNotice } from '@/stores/authStore'
import { AuthLayout } from '../components/AuthLayout'
import { PasswordInput } from '../components/PasswordInput'
import { loginErrorMessage, safeNext } from '../authErrors'

const schema = z.object({
  login: z.string().trim().min(1, 'Informe seu e-mail ou telefone.'),
  password: z.string().min(1, 'Informe sua senha.'),
})
type FormValues = z.infer<typeof schema>

const NOTICES: Record<Exclude<AuthNotice, null>, { text: string; variant: 'info' | 'success' | 'warning' }> = {
  expired: { text: 'Sua sessão expirou. Entre novamente para continuar.', variant: 'warning' },
  logged_out: { text: 'Você saiu da sua conta.', variant: 'info' },
  mfa_enabled: { text: 'Autenticação em dois fatores ativada. Entre novamente com o código do aplicativo.', variant: 'success' },
  password_reset: { text: 'Senha redefinida. Entre com a nova senha.', variant: 'success' },
}

export default function LoginPage() {
  const status = useAuthStore((s) => s.status)
  const notice = useAuthStore((s) => s.notice)
  const login = useAuthStore((s) => s.login)
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const next = safeNext(params.get('next'))
  const [serverError, setServerError] = React.useState<string | null>(null)
  const errorRef = React.useRef<HTMLDivElement>(null)

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<FormValues>({ resolver: zodResolver(schema), defaultValues: { login: '', password: '' } })

  React.useEffect(() => {
    if (serverError) errorRef.current?.focus()
  }, [serverError])

  if (status === 'booting') return <BootScreen />
  if (status === 'authenticated') return <Navigate to={next} replace />
  if (status === 'setup_required') return <Navigate to="/primeiro-acesso" replace />

  const onSubmit = async (values: FormValues) => {
    setServerError(null)
    try {
      const outcome = await login(values.login, values.password)
      const qs = next !== '/' ? `?next=${encodeURIComponent(next)}` : ''
      if (outcome === 'mfa_required') navigate(`/login/verificacao${qs}`, { replace: true })
      else if (outcome === 'setup_required') navigate(`/primeiro-acesso${qs}`, { replace: true })
      else navigate(next, { replace: true })
    } catch (e) {
      setServerError(loginErrorMessage(e))
    }
  }

  const n = notice ? NOTICES[notice] : null

  return (
    <AuthLayout
      title="Entrar no painel"
      description="Use o e-mail ou telefone cadastrado e sua senha."
      footer={
        <>
          <p className="m-0">
            Quer anunciar nas TVs da rede?{' '}
            <Link to="/cadastro-anunciante" className="inline-flex min-h-11 items-center font-semibold text-link underline-offset-4 hover:underline">
              Criar conta de anunciante
            </Link>
          </p>
          <p className="m-0">Recebeu um convite? Use o link enviado por e-mail para criar seu acesso.</p>
        </>
      }
    >
      {n && !serverError ? (
        <Alert variant={n.variant}>
          <AlertDescription className="text-text">{n.text}</AlertDescription>
        </Alert>
      ) : null}
      {serverError ? (
        <Alert variant="danger" ref={errorRef} tabIndex={-1} className="outline-none">
          <AlertDescription className="text-text">{serverError}</AlertDescription>
        </Alert>
      ) : null}
      <form onSubmit={handleSubmit(onSubmit)} noValidate className="flex flex-col gap-6">
        <FieldGroup>
          <Field invalid={!!errors.login}>
            <FieldLabel required>E-mail ou telefone</FieldLabel>
            <Input autoComplete="username" inputMode="email" autoFocus {...register('login')} />
            <FieldDescription>Telefone com DDD, somente números.</FieldDescription>
            <FieldError>{errors.login?.message}</FieldError>
          </Field>
          <Field invalid={!!errors.password}>
            <div className="flex items-center justify-between gap-3">
              <FieldLabel required>Senha</FieldLabel>
              <Link to="/esqueci-senha" className="inline-flex min-h-11 items-center text-sm font-semibold text-link underline-offset-4 hover:underline">
                Esqueci a senha
              </Link>
            </div>
            <PasswordInput autoComplete="current-password" {...register('password')} />
            <FieldError>{errors.password?.message}</FieldError>
          </Field>
        </FieldGroup>
        <Button type="submit" size="block" loading={isSubmitting} loadingText="Entrando…">
          <LogIn aria-hidden="true" />
          Entrar
        </Button>
      </form>
    </AuthLayout>
  )
}
