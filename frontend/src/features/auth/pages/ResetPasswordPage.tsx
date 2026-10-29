/** Redefinir senha com o código recebido por e-mail (contrato v1 §2: POST /auth/password/reset). */
import * as React from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { isApiError } from '@/lib/errors'
import { useAuthStore } from '@/stores/authStore'
import { authApi } from '../api'
import { AuthLayout } from '../components/AuthLayout'
import { NewPasswordFields } from '../components/NewPasswordFields'
import { newPasswordSchema } from '../passwordSchema'
import { loginErrorMessage } from '../authErrors'

const schema = newPasswordSchema(
  {
    email: z.string().trim().email('Informe um e-mail válido.'),
    code: z.string().trim().regex(/^\d{6}$/, 'O código tem 6 números.'),
  },
  (v) => v.email as string,
)
type Values = z.infer<typeof schema>

export default function ResetPasswordPage() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const setNotice = useAuthStore((s) => s.setNotice)
  const [serverError, setServerError] = React.useState<string | null>(null)
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: { email: params.get('email') ?? '', code: '', new_password: '', confirm: '' },
  })

  const onSubmit = async (v: Values) => {
    setServerError(null)
    try {
      await authApi.resetPassword(v.email, v.code, v.new_password)
      setNotice('password_reset')
      navigate('/login', { replace: true })
    } catch (e) {
      if (isApiError(e) && (e.status === 400 || e.status === 401 || e.status === 422)) {
        setServerError(e.code === 'HTTP_400' || e.code === 'HTTP_401' ? 'Código inválido ou expirado. Peça um novo código.' : e.message)
      } else setServerError(loginErrorMessage(e))
    }
  }

  return (
    <AuthLayout
      title="Criar nova senha"
      description="Digite o código enviado ao seu e-mail e escolha uma nova senha. Todas as sessões abertas serão encerradas."
      footer={
        <div className="flex flex-col items-center gap-1">
          <Button variant="tertiary" asChild>
            <Link to="/esqueci-senha">Pedir um novo código</Link>
          </Button>
          <Button variant="tertiary" asChild>
            <Link to="/login">Voltar para o login</Link>
          </Button>
        </div>
      }
    >
      {serverError ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">{serverError}</AlertDescription>
        </Alert>
      ) : null}
      <form onSubmit={handleSubmit(onSubmit)} noValidate className="flex flex-col gap-6">
        <FieldGroup>
          <Field invalid={!!errors.email}>
            <FieldLabel required>E-mail</FieldLabel>
            <Input type="email" autoComplete="email" {...register('email')} />
            <FieldError>{errors.email?.message}</FieldError>
          </Field>
          <Field invalid={!!errors.code}>
            <FieldLabel required>Código recebido</FieldLabel>
            <Input inputMode="numeric" autoComplete="one-time-code" maxLength={6} {...register('code')} />
            <FieldDescription>Vale por 15 minutos e pode ser usado uma vez.</FieldDescription>
            <FieldError>{errors.code?.message}</FieldError>
          </Field>
          <NewPasswordFields register={register} errors={errors} />
        </FieldGroup>
        <Button type="submit" size="block" loading={isSubmitting} loadingText="Salvando…">
          Salvar nova senha
        </Button>
      </form>
    </AuthLayout>
  )
}
