/**
 * Segurança financeira do PRÓPRIO usuário (W-FIN `financial/security/*`):
 * - StepUpFields: código TOTP + senha financeira (campos com label, erro junto ao campo).
 * - FinancialPasswordForm: define/troca a senha financeira (senha de login + TOTP [+ atual]).
 * - FinanceSecurityNotice: orienta quando falta 2FA ou senha financeira antes de operar.
 */
import * as React from 'react'
import { Link } from 'react-router-dom'
import { useForm, type FieldErrors, type UseFormRegister, type FieldValues, type Path } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { KeyRound, ShieldAlert } from 'lucide-react'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { http } from '@/lib/http'
import { errorMessage, isApiError } from '@/lib/errors'
import { formatDateTime } from '@/lib/format'
import { totpSchema } from './step-up'
import { securityStatusKey, type FinanceSecurityStatus } from './security-status'

// ---------------------------------------------------------------------------
interface StepUpFieldsProps<T extends FieldValues> {
  register: UseFormRegister<T>
  errors: FieldErrors<T>
  disabled?: boolean
}

export function StepUpFields<T extends FieldValues>({ register, errors, disabled }: StepUpFieldsProps<T>) {
  const totpErr = errors['totp_code' as keyof typeof errors]?.message as string | undefined
  const pwdErr = errors['financial_password' as keyof typeof errors]?.message as string | undefined
  return (
    <>
      <Field invalid={!!totpErr} disabled={disabled}>
        <FieldLabel required>Código do autenticador (TOTP)</FieldLabel>
        <Input
          inputMode="numeric"
          autoComplete="one-time-code"
          maxLength={6}
          {...register('totp_code' as Path<T>)}
        />
        <FieldDescription>6 dígitos do aplicativo autenticador. Cada código vale uma única vez.</FieldDescription>
        <FieldError>{totpErr}</FieldError>
      </Field>
      <Field invalid={!!pwdErr} disabled={disabled}>
        <FieldLabel required>Senha financeira</FieldLabel>
        <Input type="password" autoComplete="off" {...register('financial_password' as Path<T>)} />
        <FieldDescription>Senha própria para operações financeiras (diferente da senha de login).</FieldDescription>
        <FieldError>{pwdErr}</FieldError>
      </Field>
    </>
  )
}

// ---------------------------------------------------------------------------
const pwdSchema = z
  .object({
    login_password: z.string().min(1, 'Informe sua senha de login.'),
    current_financial_password: z.string().optional(),
    new_password: z.string().min(8, 'Use pelo menos 8 caracteres.').max(128, 'Senha longa demais.'),
    confirm: z.string(),
    totp_code: totpSchema,
  })
  .refine((v) => v.new_password === v.confirm, { path: ['confirm'], message: 'As senhas não conferem.' })

type PwdValues = z.infer<typeof pwdSchema>

export function FinancialPasswordForm({ alreadySet, onDone }: { alreadySet: boolean; onDone?: () => void }) {
  const qc = useQueryClient()
  const [serverError, setServerError] = React.useState<string | null>(null)
  const {
    register,
    handleSubmit,
    reset,
    setError,
    formState: { errors, isSubmitting },
  } = useForm<PwdValues>({
    resolver: zodResolver(
      alreadySet
        ? pwdSchema.refine((v) => !!v.current_financial_password, {
            path: ['current_financial_password'],
            message: 'Informe a senha financeira atual.',
          })
        : pwdSchema,
    ),
    defaultValues: { login_password: '', current_financial_password: '', new_password: '', confirm: '', totp_code: '' },
  })

  const onSubmit = async (v: PwdValues) => {
    setServerError(null)
    try {
      await http.post('/financial/security/financial-password', {
        login_password: v.login_password,
        totp_code: v.totp_code,
        new_password: v.new_password,
        current_financial_password: alreadySet ? v.current_financial_password : undefined,
      })
      reset()
      toast.success(alreadySet ? 'Senha financeira alterada.' : 'Senha financeira definida.')
      await qc.invalidateQueries({ queryKey: securityStatusKey })
      onDone?.()
    } catch (e) {
      if (isApiError(e) && e.code === 'FINANCIAL_PASSWORD_POLICY') setError('new_password', { message: e.message })
      else setServerError(errorMessage(e))
    }
  }

  return (
    <form onSubmit={handleSubmit(onSubmit)} noValidate className="flex flex-col gap-5">
      {serverError ? (
        <Alert variant="danger">
          <AlertDescription>{serverError}</AlertDescription>
        </Alert>
      ) : null}
      <FieldGroup>
        <Field invalid={!!errors.login_password}>
          <FieldLabel required>Senha de login</FieldLabel>
          <Input type="password" autoComplete="current-password" {...register('login_password')} />
          <FieldError>{errors.login_password?.message}</FieldError>
        </Field>
        {alreadySet ? (
          <Field invalid={!!errors.current_financial_password}>
            <FieldLabel required>Senha financeira atual</FieldLabel>
            <Input type="password" autoComplete="off" {...register('current_financial_password')} />
            <FieldError>{errors.current_financial_password?.message}</FieldError>
          </Field>
        ) : null}
        <Field invalid={!!errors.new_password}>
          <FieldLabel required>{alreadySet ? 'Nova senha financeira' : 'Senha financeira'}</FieldLabel>
          <Input type="password" autoComplete="new-password" {...register('new_password')} />
          <FieldDescription>Entre 8 e 128 caracteres, diferente da senha de login.</FieldDescription>
          <FieldError>{errors.new_password?.message}</FieldError>
        </Field>
        <Field invalid={!!errors.confirm}>
          <FieldLabel required>Confirme a senha financeira</FieldLabel>
          <Input type="password" autoComplete="new-password" {...register('confirm')} />
          <FieldError>{errors.confirm?.message}</FieldError>
        </Field>
        <Field invalid={!!errors.totp_code}>
          <FieldLabel required>Código do autenticador (TOTP)</FieldLabel>
          <Input inputMode="numeric" autoComplete="one-time-code" maxLength={6} {...register('totp_code')} />
          <FieldError>{errors.totp_code?.message}</FieldError>
        </Field>
      </FieldGroup>
      <Button type="submit" loading={isSubmitting} loadingText="Salvando…" className="self-start">
        <KeyRound aria-hidden="true" />
        {alreadySet ? 'Trocar senha financeira' : 'Definir senha financeira'}
      </Button>
    </form>
  )
}

export function FinanceSecurityNotice({ status, loading, error, onRetry }: {
  status: FinanceSecurityStatus | undefined
  loading?: boolean
  error?: unknown
  onRetry?: () => void
}) {
  if (loading) return <Skeleton className="h-24 w-full rounded-xl" />
  if (error) {
    return (
      <Alert variant="danger">
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <AlertTitle>Não foi possível verificar sua segurança financeira</AlertTitle>
          <AlertDescription className="flex flex-wrap items-center gap-3">
            {errorMessage(error)}
            {onRetry ? (
              <Button variant="secondary" onClick={onRetry}>
                Tentar novamente
              </Button>
            ) : null}
          </AlertDescription>
        </div>
      </Alert>
    )
  }
  if (!status) return null
  if (!status.totp_enabled) {
    return (
      <Alert variant="warning">
        <ShieldAlert aria-hidden="true" />
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <AlertTitle>Ative a verificação em duas etapas</AlertTitle>
          <AlertDescription className="flex flex-col gap-2">
            Operações financeiras exigem o código do aplicativo autenticador (TOTP).
            <Link to="/minha-conta/seguranca" className="font-semibold text-link underline-offset-4 hover:underline">
              Ir para Minha conta → Segurança
            </Link>
          </AlertDescription>
        </div>
      </Alert>
    )
  }
  if (!status.financial_password_set) {
    return (
      <Card>
        <CardHeader>
          <div className="flex flex-col gap-1">
            <CardTitle>Defina sua senha financeira</CardTitle>
            <CardDescription>
              Exigida junto com o código do autenticador para aprovar operações e solicitar saques. Só você pode
              defini-la.
            </CardDescription>
          </div>
        </CardHeader>
        <FinancialPasswordForm alreadySet={false} />
      </Card>
    )
  }
  return null
}

export function FinancialPasswordChangedAt({ status }: { status: FinanceSecurityStatus | undefined }) {
  if (!status?.financial_password_set) return null
  return (
    <p className="text-xs text-text-muted">
      Senha financeira definida{status.financial_password_changed_at ? ` em ${formatDateTime(status.financial_password_changed_at)}` : ''}.
    </p>
  )
}
