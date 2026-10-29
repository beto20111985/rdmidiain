/** Verificação em dois fatores no login (contrato v1 §2: POST /auth/mfa/verify). */
import * as React from 'react'
import { Link, Navigate, useNavigate, useSearchParams } from 'react-router-dom'
import { ShieldCheck } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { useAuthStore } from '@/stores/authStore'
import { AuthLayout } from '../components/AuthLayout'
import { mfaErrorMessage, safeNext } from '../authErrors'

export default function MfaVerifyPage() {
  const status = useAuthStore((s) => s.status)
  const mfaToken = useAuthStore((s) => s.mfaToken)
  const mfaExpiresAt = useAuthStore((s) => s.mfaExpiresAt)
  const verifyMfa = useAuthStore((s) => s.verifyMfa)
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const next = safeNext(params.get('next'))
  const [mode, setMode] = React.useState<'totp' | 'recovery'>('totp')
  const [code, setCode] = React.useState('')
  const [fieldError, setFieldError] = React.useState<string | null>(null)
  const [serverError, setServerError] = React.useState<string | null>(null)
  const [pending, setPending] = React.useState(false)

  if (status === 'authenticated') return <Navigate to={next} replace />

  const expired = !mfaToken || (mfaExpiresAt !== null && Date.now() > mfaExpiresAt)
  if (expired) {
    return (
      <AuthLayout title="Verificação expirada" description="Por segurança, o código precisa ser informado logo após a senha.">
        <Button asChild size="block">
          <Link to="/login">Voltar para o login</Link>
        </Button>
      </AuthLayout>
    )
  }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setServerError(null)
    const value = code.replace(/\s+/g, '')
    if (mode === 'totp' && !/^\d{6}$/.test(value)) {
      setFieldError('Digite os 6 números exibidos no aplicativo autenticador.')
      return
    }
    if (mode === 'recovery' && value.length < 6) {
      setFieldError('Digite um código de recuperação válido.')
      return
    }
    setFieldError(null)
    setPending(true)
    try {
      const outcome = await verifyMfa(value)
      if (outcome === 'setup_required') navigate('/primeiro-acesso', { replace: true })
      else navigate(next, { replace: true })
    } catch (err) {
      setServerError(mfaErrorMessage(err))
      setCode('')
    } finally {
      setPending(false)
    }
  }

  return (
    <AuthLayout
      title="Verificação em duas etapas"
      description={
        mode === 'totp'
          ? 'Abra o aplicativo autenticador (ex.: Google Authenticator) e digite o código de 6 números.'
          : 'Digite um dos códigos de recuperação que você guardou ao ativar a verificação. Cada código vale uma vez.'
      }
    >
      {serverError ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">{serverError}</AlertDescription>
        </Alert>
      ) : null}
      <form onSubmit={submit} noValidate className="flex flex-col gap-6">
        <Field invalid={!!fieldError}>
          <FieldLabel required>{mode === 'totp' ? 'Código do aplicativo' : 'Código de recuperação'}</FieldLabel>
          <Input
            key={mode}
            autoFocus
            value={code}
            onChange={(e) => setCode(e.target.value)}
            inputMode={mode === 'totp' ? 'numeric' : 'text'}
            autoComplete="one-time-code"
            maxLength={mode === 'totp' ? 6 : 32}
            className={mode === 'totp' ? 'text-center text-[20px] tracking-[0.4em] tabular-nums' : undefined}
          />
          {mode === 'totp' ? <FieldDescription>O código muda a cada 30 segundos.</FieldDescription> : null}
          <FieldError>{fieldError}</FieldError>
        </Field>
        <Button type="submit" size="block" loading={pending} loadingText="Verificando…">
          <ShieldCheck aria-hidden="true" />
          Verificar e entrar
        </Button>
      </form>
      <div className="flex flex-col items-center gap-1">
        <Button
          variant="tertiary"
          onClick={() => {
            setMode((m) => (m === 'totp' ? 'recovery' : 'totp'))
            setCode('')
            setFieldError(null)
          }}
        >
          {mode === 'totp' ? 'Usar um código de recuperação' : 'Usar o código do aplicativo'}
        </Button>
        <Button variant="tertiary" asChild>
          <Link to="/login">Voltar para o login</Link>
        </Button>
      </div>
    </AuthLayout>
  )
}
