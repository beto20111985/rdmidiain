/** Esqueci a senha (E2 #64, #66): código de 6 dígitos por e-mail; resposta sempre neutra (sem enumeração). */
import * as React from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { Mail } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Field, FieldError, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { isApiError } from '@/lib/errors'
import { authApi } from '../api'
import { AuthLayout } from '../components/AuthLayout'
import { loginErrorMessage } from '../authErrors'

const schema = z.object({ email: z.string().trim().email('Informe um e-mail válido.') })
type Values = z.infer<typeof schema>

export default function ForgotPasswordPage() {
  const navigate = useNavigate()
  const [sentTo, setSentTo] = React.useState<string | null>(null)
  const [serverError, setServerError] = React.useState<string | null>(null)
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { email: '' } })

  const onSubmit = async ({ email }: Values) => {
    setServerError(null)
    try {
      await authApi.forgotPassword(email)
      setSentTo(email)
    } catch (e) {
      // Só erros de transporte/limite chegam aqui; a resposta de negócio é sempre 202.
      if (isApiError(e) && (e.status === 429 || e.status === 0 || e.status >= 500)) setServerError(loginErrorMessage(e))
      else setSentTo(email)
    }
  }

  return (
    <AuthLayout
      title="Recuperar acesso"
      description="Informe o e-mail da sua conta. Enviaremos um código para criar uma nova senha."
      footer={
        <Button variant="tertiary" asChild>
          <Link to="/login">Voltar para o login</Link>
        </Button>
      }
    >
      {sentTo ? (
        <div className="flex flex-col gap-6">
          <Alert variant="success">
            <AlertDescription className="text-text">
              Se houver uma conta com <strong>{sentTo}</strong>, enviamos um código de 6 números. Ele vale por 15 minutos e
              pode ser usado uma vez.
            </AlertDescription>
          </Alert>
          <Button size="block" onClick={() => navigate(`/redefinir-senha?email=${encodeURIComponent(sentTo)}`)}>
            Já tenho o código
          </Button>
        </div>
      ) : (
        <form onSubmit={handleSubmit(onSubmit)} noValidate className="flex flex-col gap-6">
          {serverError ? (
            <Alert variant="danger">
              <AlertDescription className="text-text">{serverError}</AlertDescription>
            </Alert>
          ) : null}
          <Field invalid={!!errors.email}>
            <FieldLabel required>E-mail</FieldLabel>
            <Input type="email" autoComplete="email" autoFocus {...register('email')} />
            <FieldError>{errors.email?.message}</FieldError>
          </Field>
          <Button type="submit" size="block" loading={isSubmitting} loadingText="Enviando…">
            <Mail aria-hidden="true" />
            Enviar código
          </Button>
        </form>
      )}
    </AuthLayout>
  )
}
