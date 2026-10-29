/**
 * Passo 3 — senha financeira do titular de assento (POST /auth/setup/financial-password).
 * Exige a senha de login, o código TOTP atual e a nova senha financeira (diferente da de login).
 */
import * as React from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { Landmark } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { isApiError } from '@/lib/errors'
import { useAuthStore } from '@/stores/authStore'
import { totpSchema } from '@/components/finance/step-up'
import { authApi } from '../../api'
import { setupErrorMessage } from '../../authErrors'
import { PasswordInput } from '../PasswordInput'

const schema = z
  .object({
    login_password: z.string().min(1, 'Informe a senha de login.'),
    totp_code: totpSchema,
    financial_password: z.string().min(8, 'Use pelo menos 8 caracteres.').max(128, 'Senha longa demais.'),
    confirm: z.string(),
  })
  .refine((v) => v.financial_password === v.confirm, { path: ['confirm'], message: 'As senhas não conferem.' })
  .refine((v) => v.financial_password !== v.login_password, {
    path: ['financial_password'],
    message: 'A senha financeira deve ser diferente da senha de login.',
  })
type Values = z.infer<typeof schema>

export function FinancialPasswordStep() {
  const applySetupPassword = useAuthStore((s) => s.applySetupPassword)
  const refreshSetup = useAuthStore((s) => s.refreshSetup)
  const [serverError, setServerError] = React.useState<string | null>(null)
  const {
    register,
    handleSubmit,
    setError,
    resetField,
    formState: { errors, isSubmitting },
  } = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { login_password: '', totp_code: '', financial_password: '', confirm: '' } })

  const onSubmit = async (v: Values) => {
    setServerError(null)
    try {
      const res = await authApi.setupFinancialPassword({
        login_password: v.login_password,
        totp_code: v.totp_code.trim(),
        financial_password: v.financial_password,
      })
      await applySetupPassword(res)
    } catch (e) {
      resetField('totp_code') // TOTP é de uso único
      if (isApiError(e)) {
        if (e.code === 'FINANCIAL_PASSWORD_POLICY') {
          setError('financial_password', { message: setupErrorMessage(e) })
          return
        }
        if (e.code === 'SETUP_STEP_OUT_OF_ORDER') {
          setServerError(setupErrorMessage(e))
          await refreshSetup().catch(() => undefined)
          return
        }
      }
      setServerError(setupErrorMessage(e))
    }
  }

  return (
    <form onSubmit={handleSubmit(onSubmit)} noValidate className="flex flex-col gap-6" aria-label="Criar senha financeira">
      {serverError ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">{serverError}</AlertDescription>
        </Alert>
      ) : null}
      <FieldGroup>
        <Field invalid={!!errors.login_password}>
          <FieldLabel required>Senha de login</FieldLabel>
          <PasswordInput autoComplete="current-password" autoFocus {...register('login_password')} />
          <FieldDescription>A senha que você acabou de criar para entrar no painel.</FieldDescription>
          <FieldError>{errors.login_password?.message}</FieldError>
        </Field>
        <Field invalid={!!errors.totp_code}>
          <FieldLabel required>Código do autenticador (TOTP)</FieldLabel>
          <Input inputMode="numeric" autoComplete="one-time-code" maxLength={6} className="max-w-[180px]" {...register('totp_code')} />
          <FieldDescription>
            Use o código atual do aplicativo. Se você acabou de ativar o autenticador, aguarde o código mudar (a cada 30 segundos): o código
            usado na etapa anterior não vale de novo.
          </FieldDescription>
          <FieldError>{errors.totp_code?.message}</FieldError>
        </Field>
        <Field invalid={!!errors.financial_password}>
          <FieldLabel required>Senha financeira</FieldLabel>
          <PasswordInput autoComplete="new-password" {...register('financial_password')} />
          <FieldDescription>
            Entre 8 e 128 caracteres, diferente da senha de login. Será exigida, junto com o código do autenticador, para autorizar operações
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
      <Button type="submit" size="block" loading={isSubmitting} loadingText="Salvando…">
        <Landmark aria-hidden="true" />
        Criar senha financeira
      </Button>
    </form>
  )
}
