/**
 * Passo 1 — alterar a senha provisória (POST /auth/setup/password). Antes do TOTP não há sessão:
 * a chamada usa o token de setup e a resposta traz um NOVO token (o anterior deixa de valer).
 */
import * as React from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { KeyRound } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Field, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { isApiError } from '@/lib/errors'
import { tokenStore } from '@/lib/http'
import { useAuthStore } from '@/stores/authStore'
import { authApi } from '../../api'
import { setupErrorMessage } from '../../authErrors'
import { newPasswordSchema } from '../../passwordSchema'
import { NewPasswordFields } from '../NewPasswordFields'
import { PasswordInput } from '../PasswordInput'

const makeSchema = (email?: string | null) =>
  newPasswordSchema({ current_password: z.string().min(1, 'Informe a senha atual (provisória).') }, () => email ?? undefined)
type Values = z.infer<ReturnType<typeof makeSchema>>

export function PasswordStep() {
  const email = useAuthStore((s) => s.me?.user.email)
  const setupToken = useAuthStore((s) => s.mfaSetupToken)
  const applySetupPassword = useAuthStore((s) => s.applySetupPassword)
  const refreshSetup = useAuthStore((s) => s.refreshSetup)
  const expireSetupToken = useAuthStore((s) => s.expireSetupToken)
  const [serverError, setServerError] = React.useState<string | null>(null)
  const schema = React.useMemo(() => makeSchema(email), [email])
  const {
    register,
    handleSubmit,
    setError,
    resetField,
    formState: { errors, isSubmitting },
  } = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { current_password: '', new_password: '', confirm: '' } })

  const onSubmit = async (v: Values) => {
    setServerError(null)
    const usingSetupToken = !tokenStore.get() && !!setupToken
    try {
      const res = await authApi.setupPassword(v.current_password, v.new_password, usingSetupToken ? setupToken : null)
      await applySetupPassword(res)
    } catch (e) {
      if (isApiError(e)) {
        if (e.status === 401 && usingSetupToken) {
          expireSetupToken()
          return
        }
        if (e.code === 'INVALID_PASSWORD') {
          resetField('current_password')
          setError('current_password', { message: setupErrorMessage(e) })
          return
        }
        if (e.code === 'PASSWORD_POLICY' || e.code === 'PASSWORD_REUSED') {
          setError('new_password', { message: setupErrorMessage(e) })
          return
        }
        if (e.code === 'PASSWORD_CHANGE_NOT_REQUIRED' || e.code === 'SETUP_STEP_OUT_OF_ORDER') {
          // O servidor diz que este passo não está mais pendente: sincroniza e segue para o próximo.
          await refreshSetup().catch(() => undefined)
          return
        }
      }
      setServerError(setupErrorMessage(e))
    }
  }

  return (
    <form onSubmit={handleSubmit(onSubmit)} noValidate className="flex flex-col gap-6" aria-label="Alterar senha">
      {serverError ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">{serverError}</AlertDescription>
        </Alert>
      ) : null}
      <FieldGroup>
        <Field invalid={!!errors.current_password}>
          <FieldLabel required>Senha atual (provisória)</FieldLabel>
          <PasswordInput autoComplete="current-password" autoFocus {...register('current_password')} />
          <FieldError>{errors.current_password?.message}</FieldError>
        </Field>
        <NewPasswordFields register={register} errors={errors} />
      </FieldGroup>
      <Button type="submit" size="block" loading={isSubmitting} loadingText="Salvando…">
        <KeyRound aria-hidden="true" />
        Salvar nova senha
      </Button>
    </form>
  )
}
