/** Aceitar convite (E2 #67; contrato v1 §2: POST /auth/invites/accept → mesma resposta do login). */
import * as React from 'react'
import { Link, Navigate, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Field, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { isApiError } from '@/lib/errors'
import { useAuthStore } from '@/stores/authStore'
import { AuthLayout } from '../components/AuthLayout'
import { NewPasswordFields } from '../components/NewPasswordFields'
import { newPasswordSchema } from '../passwordSchema'
import { loginErrorMessage } from '../authErrors'

const schema = newPasswordSchema({ name: z.string().trim().min(2, 'Informe seu nome completo.') })
type Values = z.infer<typeof schema>

export default function AcceptInvitePage() {
  // Aceita `/convite/:token` e também `/convite?token=...` (formato do link enviado pelo backend).
  const { token: pathToken = '' } = useParams()
  const [searchParams] = useSearchParams()
  const token = pathToken || searchParams.get('token') || ''
  const navigate = useNavigate()
  const status = useAuthStore((s) => s.status)
  const acceptInvite = useAuthStore((s) => s.acceptInvite)
  const [serverError, setServerError] = React.useState<string | null>(null)
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { name: '', new_password: '', confirm: '' } })

  if (status === 'authenticated') return <Navigate to="/" replace />

  const onSubmit = async (v: Values) => {
    setServerError(null)
    try {
      const outcome = await acceptInvite(token, v.name, v.new_password)
      if (outcome === 'setup_required') navigate('/primeiro-acesso', { replace: true })
      else if (outcome === 'mfa_required') navigate('/login/verificacao', { replace: true })
      else navigate('/', { replace: true })
    } catch (e) {
      if (isApiError(e) && (e.status === 404 || e.status === 410 || e.status === 401)) {
        setServerError('Este convite é inválido, já foi usado ou expirou. Peça um novo convite a quem convidou você.')
      } else setServerError(loginErrorMessage(e))
    }
  }

  return (
    <AuthLayout
      title="Aceitar convite"
      description="Crie seu acesso ao painel da RD Mídia Indoor."
      footer={
        <Button variant="tertiary" asChild>
          <Link to="/login">Já tenho acesso</Link>
        </Button>
      }
    >
      {!token ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">
            O link do convite está incompleto. Abra novamente o link recebido por e-mail ou peça um novo convite.
          </AlertDescription>
        </Alert>
      ) : null}
      {serverError ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">{serverError}</AlertDescription>
        </Alert>
      ) : null}
      <form onSubmit={handleSubmit(onSubmit)} noValidate className="flex flex-col gap-6">
        <FieldGroup>
          <Field invalid={!!errors.name}>
            <FieldLabel required>Nome completo</FieldLabel>
            <Input autoComplete="name" autoFocus {...register('name')} />
            <FieldError>{errors.name?.message}</FieldError>
          </Field>
          <NewPasswordFields register={register} errors={errors} label="Senha" />
        </FieldGroup>
        <Button type="submit" size="block" loading={isSubmitting} disabled={!token} loadingText="Criando acesso…">
          Criar acesso
        </Button>
      </form>
    </AuthLayout>
  )
}
