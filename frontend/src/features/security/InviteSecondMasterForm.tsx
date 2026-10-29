/**
 * Convite do 2º Administrador Master (SPEC AÇÃO 003 §5.2): nome, e-mail e TOTP de quem convida.
 * O link de ativação vai SOMENTE ao e-mail do convidado — quem convida não vê o link, nem define
 * senha, TOTP ou senha financeira do outro Master. Só existe na aprovação DUPLA.
 */
import * as React from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Send } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { isApiError } from '@/lib/errors'
import { useAuthStore } from '@/stores/authStore'
import { securityErrorMessage } from '@/features/auth/authErrors'
import { totpSchema } from '@/components/finance/step-up'
import { securityApi } from './api'
import type { MasterInvite } from './types'

const schema = z.object({
  full_name: z.string().trim().min(3, 'Informe o nome completo do convidado.').max(255, 'Nome longo demais.'),
  email: z
    .string()
    .trim()
    .min(1, 'Informe o e-mail do convidado.')
    .max(255, 'E-mail longo demais.')
    .email('Informe um e-mail válido.'),
  totp_code: totpSchema,
})
type Values = z.infer<typeof schema>

export function InviteSecondMasterForm({
  onInvited,
  onCancel,
  cancelLabel = 'Fazer depois',
  autoFocus = false,
}: {
  onInvited?: (invite: MasterInvite) => void
  onCancel?: () => void
  cancelLabel?: string
  autoFocus?: boolean
}) {
  const qc = useQueryClient()
  const reloadMe = useAuthStore((s) => s.reloadMe)
  const [serverError, setServerError] = React.useState<string | null>(null)
  const {
    register,
    handleSubmit,
    setError,
    resetField,
    formState: { errors, isSubmitting },
  } = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { full_name: '', email: '', totp_code: '' } })

  const onSubmit = async (v: Values) => {
    setServerError(null)
    try {
      const invite = await securityApi.createInvite({ full_name: v.full_name.trim(), email: v.email.trim(), totp_code: v.totp_code.trim() })
      toast.success(`Convite enviado para ${invite.email_masked}.`)
      await qc.invalidateQueries({ queryKey: ['security'] })
      await reloadMe().catch(() => undefined)
      onInvited?.(invite)
    } catch (e) {
      resetField('totp_code') // TOTP é de uso único
      if (isApiError(e)) {
        if (e.code === 'EMAIL_ALREADY_REGISTERED' || e.code === 'EMAIL_INVALID') {
          setError('email', { message: securityErrorMessage(e) })
          return
        }
        if (e.code === 'INVALID_MFA_CODE') {
          setError('totp_code', { message: 'Código inválido ou já utilizado. Digite o código atual do aplicativo autenticador.' })
          return
        }
      }
      setServerError(securityErrorMessage(e))
    }
  }

  return (
    <form onSubmit={handleSubmit(onSubmit)} noValidate className="flex flex-col gap-5" aria-label="Convidar o segundo Administrador Master">
      {serverError ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">{serverError}</AlertDescription>
        </Alert>
      ) : null}
      <FieldGroup>
        <Field invalid={!!errors.full_name}>
          <FieldLabel required>Nome completo do convidado</FieldLabel>
          <Input autoComplete="off" autoFocus={autoFocus} {...register('full_name')} />
          <FieldError>{errors.full_name?.message}</FieldError>
        </Field>
        <Field invalid={!!errors.email}>
          <FieldLabel required>E-mail do convidado</FieldLabel>
          <Input type="email" autoComplete="off" inputMode="email" {...register('email')} />
          <FieldDescription>
            O link de ativação (válido por 72 horas) é enviado somente para este e-mail. Você não verá o link: o convidado cria a própria senha,
            o próprio autenticador e a própria senha financeira.
          </FieldDescription>
          <FieldError>{errors.email?.message}</FieldError>
        </Field>
        <Field invalid={!!errors.totp_code}>
          <FieldLabel required>Seu código do autenticador (TOTP)</FieldLabel>
          <Input inputMode="numeric" autoComplete="one-time-code" maxLength={6} className="max-w-[180px]" {...register('totp_code')} />
          <FieldDescription>Confirma que é você quem está convidando. Cada código vale uma única vez.</FieldDescription>
          <FieldError>{errors.totp_code?.message}</FieldError>
        </Field>
      </FieldGroup>
      <div className="flex flex-wrap gap-3">
        <Button type="submit" loading={isSubmitting} loadingText="Enviando convite…">
          <Send aria-hidden="true" />
          Enviar convite
        </Button>
        {onCancel ? (
          <Button type="button" variant="secondary" onClick={onCancel} disabled={isSubmitting}>
            {cancelLabel}
          </Button>
        ) : null}
      </div>
    </form>
  )
}
