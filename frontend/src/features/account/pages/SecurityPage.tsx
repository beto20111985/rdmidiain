/** Segurança: trocar senha (POST /auth/password/change) e estado/ativação do 2FA. */
import * as React from 'react'
import { Link } from 'react-router-dom'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { toast } from 'sonner'
import { KeyRound, ShieldCheck } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Field, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { useAuthStore } from '@/stores/authStore'
import { errorMessage, isApiError } from '@/lib/errors'
import { authApi } from '@/features/auth/api'
import { PasswordInput } from '@/features/auth/components/PasswordInput'
import { NewPasswordFields } from '@/features/auth/components/NewPasswordFields'
import { newPasswordSchema } from '@/features/auth/passwordSchema'

export default function SecurityPage() {
  const me = useAuthStore((s) => s.me)
  const reloadMe = useAuthStore((s) => s.reloadMe)
  const mustChange = Boolean(me?.must_change_password ?? me?.user.must_change_password)
  const email = me?.user.email
  const schema = React.useMemo(
    () => newPasswordSchema({ current_password: z.string().min(1, 'Informe a senha atual.') }, () => email),
    [email],
  )
  type Values = z.infer<typeof schema>
  const [serverError, setServerError] = React.useState<string | null>(null)
  const {
    register,
    handleSubmit,
    reset,
    formState: { errors, isSubmitting },
  } = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { current_password: '', new_password: '', confirm: '' } })

  const onSubmit = async (v: Values) => {
    setServerError(null)
    try {
      await authApi.changePassword(v.current_password, v.new_password)
      reset()
      toast.success('Senha alterada. As outras sessões foram encerradas.')
      if (mustChange) await reloadMe()
    } catch (e) {
      setServerError(isApiError(e) && (e.status === 400 || e.status === 401) && e.code.startsWith('HTTP_') ? 'Senha atual incorreta.' : errorMessage(e))
    }
  }

  return (
    <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
      <PageHeader title="Segurança" breadcrumbs={[{ label: 'Minha conta', to: '/minha-conta' }, { label: 'Segurança' }]} />
      <Card>
        <CardHeader>
          <div className="flex flex-col gap-1">
            <CardTitle>Trocar senha</CardTitle>
            <CardDescription>Use pelo menos 10 caracteres, com letras e números.</CardDescription>
          </div>
        </CardHeader>
        {mustChange ? (
          <Alert variant="warning">
            <AlertDescription className="text-text">
              Sua senha é provisória. Defina uma nova senha para continuar usando o painel.
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
            <Field invalid={!!errors.current_password}>
              <FieldLabel required>Senha atual</FieldLabel>
              <PasswordInput autoComplete="current-password" {...register('current_password')} />
              <FieldError>{errors.current_password?.message}</FieldError>
            </Field>
            <NewPasswordFields register={register} errors={errors} />
          </FieldGroup>
          <Button type="submit" loading={isSubmitting} className="self-start">
            <KeyRound aria-hidden="true" />
            Salvar nova senha
          </Button>
        </form>
      </Card>

      <Card>
        <CardHeader>
          <div className="flex flex-col gap-1">
            <CardTitle>Verificação em duas etapas</CardTitle>
            <CardDescription>Código do aplicativo autenticador a cada login.</CardDescription>
          </div>
          {me?.user.mfa_enabled ? <Badge tone="success">Ativada</Badge> : <Badge tone="warning">Desativada</Badge>}
        </CardHeader>
        {me?.user.mfa_enabled ? (
          <p className="m-0 text-sm text-text-muted">
            Sua conta está protegida. Se perder o celular, use um dos códigos de recuperação guardados.
            {me.mfa.required ? ' Seu perfil exige esta proteção; ela não pode ser desativada.' : ''}
          </p>
        ) : (
          <Button asChild className="self-start">
            <Link to="/login/configurar-2fa">
              <ShieldCheck aria-hidden="true" />
              Ativar verificação em duas etapas
            </Link>
          </Button>
        )}
      </Card>
    </div>
  )
}
