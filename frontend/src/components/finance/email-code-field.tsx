/**
 * Campo do código por e-mail + botão "Enviar código por e-mail" com espera visível (contador
 * LOCAL). Erros de envio aparecem junto ao botão; o resto do formulário é preservado.
 */
import * as React from 'react'
import type { UseFormRegisterReturn } from 'react-hook-form'
import { MailCheck } from 'lucide-react'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { errorMessage } from '@/lib/errors'
import { formatTime } from '@/lib/format'
import { EMAIL_CODE_RESEND_SECONDS, emailCodeErrorInfo, formatCountdown, useCountdown } from './email-code'

export interface EmailCodeFieldProps {
  registration: UseFormRegisterReturn
  error?: string
  disabled?: boolean
  /** Pode enviar agora (ex.: valor do saque válido). */
  canSend?: boolean
  cannotSendReason?: string
  onSend: () => Promise<{ sent?: boolean; expires_in: number }>
  /** Muda quando o código atual deixa de valer (valor alterado, código inválido) → pede novo envio. */
  resetSignal?: number
  /** Texto explicativo do vínculo do código (ex.: "vale só para este valor"). */
  hint?: string
}

export function EmailCodeField({ registration, error, disabled, canSend = true, cannotSendReason, onSend, resetSignal = 0, hint }: EmailCodeFieldProps) {
  const { remaining, start: startCooldown, reset: resetCooldown } = useCountdown()
  const [sending, setSending] = React.useState(false)
  const [expiresAt, setExpiresAt] = React.useState<Date | null>(null)
  const [sendError, setSendError] = React.useState<string | null>(null)
  const first = React.useRef(true)

  React.useEffect(() => {
    if (first.current) {
      first.current = false
      return
    }
    setExpiresAt(null)
    resetCooldown()
  }, [resetSignal, resetCooldown])

  const send = async () => {
    setSendError(null)
    setSending(true)
    try {
      const r = await onSend()
      setExpiresAt(new Date(Date.now() + r.expires_in * 1000))
      startCooldown(EMAIL_CODE_RESEND_SECONDS)
    } catch (e) {
      const info = emailCodeErrorInfo(e)
      setSendError(info?.message ?? errorMessage(e))
      if (info?.cooldownSeconds) startCooldown(info.cooldownSeconds)
    } finally {
      setSending(false)
    }
  }

  const waiting = remaining > 0
  return (
    <Field invalid={!!error} disabled={disabled}>
      <FieldLabel required>Código enviado por e-mail</FieldLabel>
      <div className="flex flex-wrap items-start gap-2">
        <Input inputMode="numeric" autoComplete="one-time-code" maxLength={6} className="max-w-[180px]" {...registration} />
        <Button
          type="button"
          variant="secondary"
          onClick={() => void send()}
          loading={sending}
          loadingText="Enviando…"
          disabled={disabled || waiting || !canSend}
        >
          <MailCheck aria-hidden="true" />
          {waiting ? `Reenviar em ${formatCountdown(remaining)}` : expiresAt ? 'Reenviar código' : 'Enviar código por e-mail'}
        </Button>
      </div>
      <FieldDescription>
        {expiresAt
          ? `Código enviado para o seu e-mail; vale até ${formatTime(expiresAt)} (10 min) e uma única vez.`
          : !canSend && cannotSendReason
            ? cannotSendReason
            : 'Exigido além do código do autenticador e da senha financeira.'}
        {hint ? ` ${hint}` : ''}
      </FieldDescription>
      {sendError ? (
        <p role="alert" className="m-0 text-xs font-medium text-danger">
          {sendError}
        </p>
      ) : null}
      <FieldError>{error}</FieldError>
    </Field>
  )
}
