/**
 * Diálogo GUIADO de mudança do modo de aprovação financeira (SPEC AÇÃO 003 §11.7) — NÃO é um switch.
 *   1. Consequências (lista real + "Entendi")  2. Motivo (≥ 20) e confirmação  3. (só Única → Dupla) autorização.
 *  - Única → Dupla: cria o pedido `APPROVAL_MODE_CHANGE` e o próprio Master o AUTORIZA (TOTP + senha financeira +
 *    código por e-mail). Efeito imediato; o 2º Master passa a ser obrigatório.
 *  - Dupla → Única: cria o pedido com `step_up` (TOTP + senha financeira) e fica "aguardando aprovação do outro
 *    Administrador Master" (que aprova com os três fatores em Financeiro → Aprovações). Impossível unilateralmente.
 * O servidor valida tudo; aqui o fluxo, os textos e os fatores.
 */
import * as React from 'react'
import { Link } from 'react-router-dom'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, ShieldAlert } from 'lucide-react'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Textarea } from '@/components/ui/textarea'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { StepProgress } from '@/features/auth/components/StepProgress'
import { StepUpFields } from '@/components/finance/security'
import { ApprovalErrorAlert } from '@/components/finance/approval-errors'
import { RequestFactorsForm } from '@/components/finance/request-factors-form'
import { financialPasswordSchema, stepUpFieldFromError, totpSchema } from '@/components/finance/step-up'
import { useFinanceReady } from '@/components/finance/security-status'
import { APPROVAL_MODE_LABEL } from '@/components/finance/approval-utils'
import { financeApi, type ApprovalRequest } from '@/pages/finance/api'
import { isApiError } from '@/lib/errors'
import { newRequestId } from '@/lib/http'
import { useAuthStore } from '@/stores/authStore'
import type { ApprovalModeCode } from './types'

export const MODE_CHANGE_MIN_REASON = 20

interface Copy {
  title: string
  consequences: string[]
  factors: string
  submit: string
}

function copyFor(from: ApprovalModeCode): Copy {
  if (from === 'SINGLE') {
    return {
      title: 'Solicitar mudança para aprovação Dupla',
      consequences: [
        'Cada operação financeira sensível passa a ser solicitada por um Administrador Master e aprovada pelo outro.',
        'É obrigatório cadastrar e ativar o segundo Administrador Master. Até a ativação, as operações que exigem dupla aprovação ficam bloqueadas.',
        'Pedidos de aprovação ainda pendentes na aprovação única são cancelados.',
        'A mudança fica registrada na trilha de segurança da instância e os Masters são notificados.',
        'Para voltar à aprovação única será preciso a aprovação do outro Administrador Master.',
      ],
      factors: 'Você confirma esta mudança com código do autenticador (TOTP), senha financeira e código enviado por e-mail.',
      submit: 'Continuar para a autorização',
    }
  }
  return {
    title: 'Solicitar mudança para aprovação Única',
    consequences: [
      'Um único Administrador Master passa a autorizar cada operação financeira, com autenticador, senha financeira e código por e-mail. Isso reduz a proteção contra erro ou uso indevido de uma conta.',
      'A mudança só vale depois que o outro Administrador Master aprovar o pedido (autenticador, senha financeira e código por e-mail).',
      'Pedidos de dupla aprovação ainda pendentes são cancelados quando a mudança for aplicada.',
      'A mudança fica registrada na trilha de segurança da instância e os Masters são notificados.',
    ],
    factors: 'Para solicitar, você confirma com código do autenticador (TOTP) e senha financeira.',
    submit: 'Solicitar mudança',
  }
}

type Phase = 'consequences' | 'confirm' | 'authorize' | 'done'

function schemaFor(from: ApprovalModeCode) {
  const reason = z.string().trim().min(MODE_CHANGE_MIN_REASON, `Explique o motivo (mínimo ${MODE_CHANGE_MIN_REASON} caracteres).`).max(2000)
  return from === 'DUAL'
    ? z.object({ reason, totp_code: totpSchema, financial_password: financialPasswordSchema })
    : z.object({ reason, totp_code: z.string().optional(), financial_password: z.string().optional() })
}
type Values = { reason: string; totp_code?: string; financial_password?: string }

export interface ApprovalModeChangeDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Modo atual da instância (o alvo é o outro). */
  currentMode: ApprovalModeCode
}

export function ApprovalModeChangeDialog({ open, onOpenChange, currentMode }: ApprovalModeChangeDialogProps) {
  const qc = useQueryClient()
  const reloadMe = useAuthStore((s) => s.reloadMe)
  const { ready } = useFinanceReady(open)
  const target: ApprovalModeCode = currentMode === 'SINGLE' ? 'DUAL' : 'SINGLE'
  const copy = copyFor(currentMode)
  const [phase, setPhase] = React.useState<Phase>('consequences')
  const [ack, setAck] = React.useState(false)
  const [serverError, setServerError] = React.useState<unknown>(null)
  const [request, setRequest] = React.useState<ApprovalRequest | null>(null)
  const idem = React.useRef<string | null>(null)
  const schema = React.useMemo(() => schemaFor(currentMode), [currentMode])
  const {
    register,
    handleSubmit,
    reset,
    resetField,
    setError,
    watch,
    formState: { errors, isSubmitting },
  } = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { reason: '', totp_code: '', financial_password: '' } })
  const reasonLength = (watch('reason') ?? '').trim().length

  React.useEffect(() => {
    if (!open) return
    setPhase('consequences')
    setAck(false)
    setServerError(null)
    setRequest(null)
    idem.current = null
    reset({ reason: '', totp_code: '', financial_password: '' })
  }, [open, reset])

  const refreshAll = async () => {
    await qc.invalidateQueries({ queryKey: ['security'] })
    await qc.invalidateQueries({ queryKey: ['finance'] })
    await reloadMe().catch(() => undefined)
  }

  const onSubmit = async (v: Values) => {
    setServerError(null)
    try {
      const created = await financeApi.createApproval(
        {
          action_type: 'APPROVAL_MODE_CHANGE',
          payload: { target_mode: target },
          reason: v.reason.trim(),
          ...(currentMode === 'DUAL' ? { step_up: { totp_code: (v.totp_code ?? '').trim(), financial_password: v.financial_password ?? '' } } : {}),
        },
        (idem.current ??= newRequestId()),
      )
      setRequest(created)
      await qc.invalidateQueries({ queryKey: ['security'] })
      if (currentMode === 'SINGLE' && created.status === 'PENDING') setPhase('authorize')
      else {
        await refreshAll()
        setPhase('done')
      }
    } catch (e) {
      resetField('totp_code') // TOTP é de uso único
      if (isApiError(e)) {
        if (e.code === 'REASON_REQUIRED') {
          setError('reason', { message: e.message })
          return
        }
        const field = stepUpFieldFromError(e.code)
        if (field === 'totp_code' || field === 'financial_password') {
          if (e.code !== 'FINANCIAL_STEP_UP_INVALID') {
            setError(field, { message: e.message })
            return
          }
        }
      }
      setServerError(e)
    }
  }

  const progress = [
    { key: 'consequences', label: 'Consequências' },
    { key: 'confirm', label: 'Motivo e confirmação' },
    ...(currentMode === 'SINGLE' ? [{ key: 'authorize', label: 'Autorização' }] : []),
  ]
  const order = ['consequences', 'confirm', 'authorize', 'done']
  const activeIndex = Math.min(order.indexOf(phase), progress.length - 1)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        {phase === 'done' ? (
          <>
            <DialogHeader>
              <DialogTitle>{currentMode === 'SINGLE' ? 'Modo de aprovação alterado' : 'Solicitação enviada'}</DialogTitle>
              <DialogDescription>
                {currentMode === 'SINGLE'
                  ? 'A instalação agora usa dupla aprovação financeira. Cadastre e ative o segundo Administrador Master para liberar as operações que exigem dupla aprovação.'
                  : 'Aguardando a aprovação do outro Administrador Master. A mudança só vale depois que ele aprovar em Financeiro → Aprovações.'}
              </DialogDescription>
            </DialogHeader>
            <Alert variant="success">
              <CheckCircle2 aria-hidden="true" />
              <AlertDescription className="text-text">
                {currentMode === 'SINGLE'
                  ? `Modo em vigor: aprovação ${APPROVAL_MODE_LABEL.DUAL.toLowerCase()}.`
                  : 'Você pode acompanhar o pedido nesta página e em Financeiro → Aprovações.'}
              </AlertDescription>
            </Alert>
            <DialogFooter>
              {currentMode === 'DUAL' && request ? (
                <Button asChild variant="secondary">
                  <Link to={`/financeiro/aprovacoes?pedido=${encodeURIComponent(request.id)}`}>Abrir o pedido</Link>
                </Button>
              ) : null}
              <Button onClick={() => onOpenChange(false)}>Fechar</Button>
            </DialogFooter>
          </>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>{copy.title}</DialogTitle>
              <DialogDescription>
                Modo atual: aprovação {APPROVAL_MODE_LABEL[currentMode].toLowerCase()}. Leia as consequências antes de continuar.
              </DialogDescription>
            </DialogHeader>
            <StepProgress
              label="Etapas da mudança de modo"
              steps={progress.map((p, i) => ({ key: p.key, label: p.label, state: i < activeIndex ? 'done' : i === activeIndex ? 'current' : 'pending' }))}
            />

            {phase === 'consequences' ? (
              <div className="flex flex-col gap-5">
                <Alert variant="warning">
                  <ShieldAlert aria-hidden="true" />
                  <div className="flex min-w-0 flex-1 flex-col gap-2">
                    <AlertTitle>O que muda</AlertTitle>
                    <ul className="m-0 flex list-disc flex-col gap-1.5 pl-5 text-sm text-text">
                      {copy.consequences.map((c) => (
                        <li key={c}>{c}</li>
                      ))}
                    </ul>
                  </div>
                </Alert>
                <p className="m-0 text-sm text-text-muted">{copy.factors}</p>
                <Field orientation="horizontal" className="min-h-11 flex-nowrap">
                  <Checkbox checked={ack} onCheckedChange={(v) => setAck(v === true)} />
                  <FieldLabel className="min-w-0 flex-1 font-medium">Entendi as consequências desta mudança</FieldLabel>
                </Field>
                <DialogFooter>
                  <Button variant="secondary" onClick={() => onOpenChange(false)}>
                    Cancelar
                  </Button>
                  <Button disabled={!ack} onClick={() => setPhase('confirm')}>
                    Continuar
                  </Button>
                </DialogFooter>
              </div>
            ) : null}

            {phase === 'confirm' ? (
              <form onSubmit={handleSubmit(onSubmit)} noValidate className="flex flex-col gap-5" aria-label="Motivo e confirmação">
                <ApprovalErrorAlert error={serverError} />
                {!ready ? (
                  <Alert variant="warning">
                    <AlertDescription className="text-text">
                      Para continuar é preciso ter a verificação em duas etapas e a senha financeira configuradas.
                    </AlertDescription>
                  </Alert>
                ) : null}
                <FieldGroup>
                  <Field invalid={!!errors.reason}>
                    <FieldLabel required>Motivo da mudança</FieldLabel>
                    <Textarea rows={4} maxLength={2000} autoFocus {...register('reason')} />
                    <FieldDescription>
                      Mínimo de {MODE_CHANGE_MIN_REASON} caracteres ({reasonLength}/{MODE_CHANGE_MIN_REASON}). Fica registrado na auditoria e é visto na aprovação.
                    </FieldDescription>
                    <FieldError>{errors.reason?.message}</FieldError>
                  </Field>
                  {currentMode === 'DUAL' ? <StepUpFields register={register} errors={errors} disabled={!ready} /> : null}
                </FieldGroup>
                <DialogFooter>
                  <Button type="button" variant="secondary" onClick={() => setPhase('consequences')} disabled={isSubmitting}>
                    Voltar
                  </Button>
                  <Button type="submit" loading={isSubmitting} loadingText="Enviando…" disabled={!ready}>
                    {copy.submit}
                  </Button>
                </DialogFooter>
              </form>
            ) : null}

            {phase === 'authorize' && request ? (
              <div className="flex flex-col gap-4">
                <p className="m-0 text-sm text-text">
                  Pedido criado. Para aplicar a mudança, autorize com o código do autenticador, a senha financeira e o código enviado por e-mail.
                </p>
                <RequestFactorsForm
                  request={request}
                  kind="authorize"
                  ready={ready}
                  showTitle={false}
                  onDone={() => {
                    void refreshAll()
                    setPhase('done')
                  }}
                />
                <p className="m-0 text-xs text-text-muted">
                  Se fechar agora, o pedido continua pendente em Financeiro → Aprovações até expirar.
                </p>
              </div>
            ) : null}
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
