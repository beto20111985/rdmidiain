/**
 * Fatores para decidir um pedido de aprovação financeira (TOTP + senha financeira + código por e-mail):
 *  - kind="approve"   → DUPLA: o outro Admin Master aprova (`POST …/approve`);
 *  - kind="authorize" → ÚNICA: o próprio solicitante autoriza (`POST …/authorize`).
 * O servidor revalida tudo (modo, solicitante, hash, fatores). O TOTP é de uso único: sempre limpo
 * após o envio; a senha financeira é preservada.
 */
import * as React from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { CheckCircle2, ShieldCheck } from 'lucide-react'
import { FieldGroup } from '@/components/ui/field'
import { Button } from '@/components/ui/button'
import { StepUpFields } from '@/components/finance/security'
import { EmailCodeField } from '@/components/finance/email-code-field'
import { emailCodeErrorInfo } from '@/components/finance/email-code'
import { emailCodeSchema, financialPasswordSchema, stepUpFieldFromError, totpSchema } from '@/components/finance/step-up'
import { ApprovalErrorAlert } from '@/components/finance/approval-errors'
import { financeApi, type ApprovalRequest } from '@/pages/finance/api'
import { errorMessage, isApiError } from '@/lib/errors'

// Código por e-mail OBRIGATÓRIO em toda aprovação/autorização (W-FIN pós-janela d), além de TOTP + senha financeira.
const schema = z.object({
  totp_code: totpSchema,
  financial_password: financialPasswordSchema,
  email_code: emailCodeSchema,
})
type Values = z.infer<typeof schema>

/** Erros que indicam que o modo/estado do pedido mudou: a tela precisa recarregar. */
const REFRESH_CODES = [
  'APPROVAL_MODE_CHANGED',
  'APPROVAL_MODE_SINGLE',
  'APPROVAL_MODE_DUAL',
  'APPROVAL_NOT_PENDING',
  'APPROVAL_EXPIRED',
  'APPROVER_NOT_ACTIVE_MASTER',
  'SELF_AUTHORIZER_INVALID',
  'REQUESTER_NOT_ACTIVE_MASTER',
  'SECOND_MASTER_REQUIRED',
]

export interface RequestFactorsFormProps {
  request: ApprovalRequest
  kind: 'approve' | 'authorize'
  /** 2FA + senha financeira configurados (habilita o formulário). */
  ready: boolean
  onDone: (result: ApprovalRequest) => void
  /** Texto de ajuda sob o título (opcional). */
  hint?: React.ReactNode
  /** Mostra o título "Aprovar"/"Autorizar" (desligue quando o contêiner já tem título). */
  showTitle?: boolean
}

export function RequestFactorsForm({ request, kind, ready, onDone, hint, showTitle = true }: RequestFactorsFormProps) {
  const qc = useQueryClient()
  const authorize = kind === 'authorize'
  const [serverError, setServerError] = React.useState<unknown>(null)
  const [codeReset, setCodeReset] = React.useState(0)
  const {
    register,
    handleSubmit,
    setError,
    resetField,
    formState: { errors, isSubmitting },
  } = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: { totp_code: '', financial_password: '', email_code: '' },
  })

  const sendCode = async () => {
    const r = await financeApi.approvalEmailCode(request.id)
    toast.success('Código enviado para o seu e-mail.')
    return r
  }

  const onSubmit = async (v: Values) => {
    setServerError(null)
    const body = {
      totp_code: v.totp_code.trim(),
      financial_password: v.financial_password,
      payload_sha256: request.payload_sha256,
      email_code: v.email_code.trim(),
    }
    try {
      const res = authorize ? await financeApi.authorize(request.id, body) : await financeApi.approve(request.id, body)
      toast.success(
        authorize
          ? res.execution_state === 'EXECUTED'
            ? 'Operação autorizada e executada.'
            : 'Operação autorizada. A execução segue no servidor — acompanhe o status.'
          : res.execution_state === 'EXECUTED'
            ? 'Pedido aprovado e executado.'
            : 'Pedido aprovado. A execução segue no servidor — acompanhe o status.',
      )
      await qc.invalidateQueries({ queryKey: ['finance'] })
      await qc.invalidateQueries({ queryKey: ['security'] })
      onDone(res)
    } catch (e) {
      // O TOTP é de uso único: sempre pedir um novo. Senha financeira e demais campos são preservados.
      resetField('totp_code')
      const codeInfo = emailCodeErrorInfo(e)
      if (codeInfo) {
        if (codeInfo.resetCode) {
          resetField('email_code')
          setCodeReset((n) => n + 1)
        }
        if (codeInfo.target === 'email_code') setError('email_code', { message: codeInfo.message })
        else setServerError(codeInfo.message)
        return
      }
      if (isApiError(e)) {
        const field = stepUpFieldFromError(e.code)
        if (e.code === 'FINANCIAL_STEP_UP_INVALID') {
          setServerError(e)
        } else if (field) {
          setError(field, { message: e.message })
        } else if (e.code === 'APPROVAL_PAYLOAD_MISMATCH' || e.code === 'APPROVAL_PAYLOAD_TAMPERED') {
          setServerError(`${e.message} O pedido foi recarregado.`)
          await qc.invalidateQueries({ queryKey: ['finance'] })
        } else {
          setServerError(e)
          if (REFRESH_CODES.includes(e.code)) await qc.invalidateQueries({ queryKey: ['finance'] })
        }
      } else setServerError(errorMessage(e))
    }
  }

  const noun = authorize ? 'Autorizar' : 'Aprovar'
  return (
    <form onSubmit={handleSubmit(onSubmit)} noValidate className="flex flex-col gap-4" aria-label={authorize ? 'Autorizar operação' : 'Aprovar pedido'}>
      {showTitle ? <h3 className="text-title-card text-text">{noun}</h3> : null}
      {hint ? <p className="m-0 text-sm text-text-muted">{hint}</p> : null}
      <ApprovalErrorAlert error={serverError} />
      <FieldGroup>
        <StepUpFields register={register} errors={errors} disabled={!ready} />
        <EmailCodeField
          registration={register('email_code')}
          error={errors.email_code?.message}
          disabled={!ready}
          onSend={sendCode}
          resetSignal={codeReset}
          hint={authorize ? 'Vale só para esta operação.' : 'Vale só para este pedido.'}
        />
      </FieldGroup>
      <p className="m-0 text-xs text-text-muted">
        Você {authorize ? 'autoriza' : 'aprova'} exatamente o conteúdo exibido (SHA-256{' '}
        <span className="font-mono">{request.payload_sha256.slice(0, 16)}…</span>).
      </p>
      <Button type="submit" loading={isSubmitting} loadingText={authorize ? 'Autorizando…' : 'Aprovando…'} disabled={!ready} className="self-start">
        {authorize ? <ShieldCheck aria-hidden="true" /> : <CheckCircle2 aria-hidden="true" />}
        {authorize ? 'Autorizar operação' : 'Aprovar pedido'}
      </Button>
    </form>
  )
}
