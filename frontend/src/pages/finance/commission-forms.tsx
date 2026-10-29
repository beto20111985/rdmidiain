/**
 * Pedidos de aprovação ligados à comissão (só Admin Master solicitam; dupla ou única conforme a instância):
 * - SETTING_CHANGE: modelo (A/B) ou pool (bps) da instância;
 * - COMMISSION_EXCEPTION: taxa/peso/recebimento/pagamento automático de um estabelecimento;
 * - COMMISSION_ADJUSTMENT: crédito (+) ou débito (−) manual auditado.
 */
import * as React from 'react'
import { Controller, useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { useQueryClient } from '@tanstack/react-query'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { bpsToInput, parseBRLToCents, parsePercentToBps } from '@/components/finance/money'
import { approvalCopy, toastApprovalCreated, useInstanceApprovalMode } from '@/components/finance/approval-utils'
import { ApprovalErrorAlert } from '@/components/finance/approval-errors'
import { useAuthorizationChain } from '@/components/finance/authorize-chain'
import { isIsoDate } from '@/components/finance/period'
import { newRequestId } from '@/lib/http'
import { financeApi, type CommissionSettings } from './api'

const reason = z.string().trim().min(10, 'Explique o motivo (mínimo 10 caracteres).').max(2000)

function useSubmitApproval(onDone: () => void) {
  const qc = useQueryClient()
  const [error, setError] = React.useState<unknown>(null)
  const chain = useAuthorizationChain(onDone)
  const resetChain = chain.reset
  const copy = approvalCopy(useInstanceApprovalMode().mode)
  /** Limpa erro e a etapa de autorização (ao abrir o diálogo). */
  const clearState = React.useCallback(() => {
    setError(null)
    resetChain()
  }, [resetChain])
  const submit = async (action_type: string, payload: Record<string, unknown>, why: string) => {
    setError(null)
    try {
      const r = await financeApi.createApproval({ action_type, payload, reason: why }, newRequestId())
      await qc.invalidateQueries({ queryKey: ['finance'] })
      // Aprovação ÚNICA: segue para a autorização no mesmo diálogo.
      if (chain.handleCreated(r)) return
      toastApprovalCreated(r)
      onDone()
    } catch (e) {
      setError(e)
    }
  }
  return { submit, error, clearState, chain, copy }
}

// ---------------------------------------------------------------------------
const settingSchema = z
  .object({ what: z.enum(['model', 'pool']), model: z.string().optional(), pool: z.string().optional(), reason })
  .superRefine((v, ctx) => {
    if (v.what === 'model' && !v.model) ctx.addIssue({ code: 'custom', path: ['model'], message: 'Escolha o modelo.' })
    if (v.what === 'pool' && parsePercentToBps(v.pool) === null) ctx.addIssue({ code: 'custom', path: ['pool'], message: 'Percentual inválido (0 a 100).' })
  })
type SettingValues = z.infer<typeof settingSchema>

export function CommissionSettingDialog({ open, onOpenChange, currentModel, poolBps }: { open: boolean; onOpenChange: (o: boolean) => void; currentModel: string | null; poolBps: number | null }) {
  const { submit, error, clearState, chain, copy } = useSubmitApproval(() => onOpenChange(false))
  const { register, control, handleSubmit, watch, reset, formState: { errors, isSubmitting } } = useForm<SettingValues>({ resolver: zodResolver(settingSchema) })
  React.useEffect(() => {
    if (open) {
      clearState()
      reset({ what: 'model', model: currentModel ?? undefined, pool: bpsToInput(poolBps), reason: '' })
    }
  }, [open, currentModel, poolBps, reset, clearState])
  const what = watch('what')
  const onSubmit = (v: SettingValues) =>
    v.what === 'model'
      ? submit('SETTING_CHANGE', { key: 'commission.model', value: v.model }, v.reason)
      : submit('SETTING_CHANGE', { key: 'commission.pool_bps', value: parsePercentToBps(v.pool) }, v.reason)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        {chain.step ?? (
          <>
        <DialogHeader>
          <DialogTitle>Alterar configuração de comissão</DialogTitle>
          <DialogDescription>Vale para pedidos pagos depois da aprovação; pedidos já pagos mantêm os termos congelados.</DialogDescription>
        </DialogHeader>
        <ApprovalErrorAlert error={error} />
        <form id="comm-setting" onSubmit={handleSubmit(onSubmit)} noValidate>
          <FieldGroup>
            <Field>
              <FieldLabel id="comm-what">O que alterar</FieldLabel>
              <Controller
                control={control}
                name="what"
                render={({ field }) => (
                  <ToggleGroup type="single" value={field.value} onValueChange={(v) => v && field.onChange(v)} aria-labelledby="comm-what" className="self-start">
                    <ToggleGroupItem value="model">Modelo</ToggleGroupItem>
                    <ToggleGroupItem value="pool">Pool (%)</ToggleGroupItem>
                  </ToggleGroup>
                )}
              />
            </Field>
            {what === 'model' ? (
              <Field invalid={!!errors.model}>
                <FieldLabel id="comm-model" required>
                  Modelo
                </FieldLabel>
                <Controller
                  control={control}
                  name="model"
                  render={({ field }) => (
                    <ToggleGroup type="single" value={field.value ?? ''} onValueChange={(v) => v && field.onChange(v)} aria-labelledby="comm-model" className="self-start">
                      <ToggleGroupItem value="A_POOL">A — pool fixo</ToggleGroupItem>
                      <ToggleGroupItem value="B_ATTRIBUTED">B — receita atribuída</ToggleGroupItem>
                    </ToggleGroup>
                  )}
                />
                <FieldError>{errors.model?.message}</FieldError>
              </Field>
            ) : (
              <Field invalid={!!errors.pool}>
                <FieldLabel required>Pool de comissão (%)</FieldLabel>
                <Input inputMode="decimal" className="max-w-[160px]" {...register('pool')} />
                <FieldDescription>Padrão 5%. A soma das comissões nunca ultrapassa o pool.</FieldDescription>
                <FieldError>{errors.pool?.message}</FieldError>
              </Field>
            )}
            <Field invalid={!!errors.reason}>
              <FieldLabel required>{copy.reasonLabel}</FieldLabel>
              <Textarea rows={3} {...register('reason')} />
              <FieldError>{errors.reason?.message}</FieldError>
            </Field>
          </FieldGroup>
        </form>
        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Cancelar
          </Button>
          <Button type="submit" form="comm-setting" loading={isSubmitting} loadingText="Enviando…">
            {copy.submitLabel}
          </Button>
        </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}

// ---------------------------------------------------------------------------
const exceptionSchema = z.object({
  receives_commission: z.boolean(),
  auto_payout: z.boolean(),
  rate: z.string().refine((v) => !v || parsePercentToBps(v) !== null, 'Percentual inválido.'),
  weight: z.string().refine((v) => !v || /^\d{1,3}([,.]\d{1,3})?$/.test(v.trim()), 'Peso inválido (ex.: 1 ou 1,5).'),
  valid_from: z.string().refine((v) => !v || isIsoDate(v), 'Data inválida.'),
  reason,
})
type ExceptionValues = z.infer<typeof exceptionSchema>

export function CommissionExceptionDialog({ settings, establishmentName, onOpenChange }: { settings: CommissionSettings | null; establishmentName: string; onOpenChange: (o: boolean) => void }) {
  const { submit, error, clearState, chain, copy } = useSubmitApproval(() => onOpenChange(false))
  const { register, control, handleSubmit, reset, formState: { errors, isSubmitting } } = useForm<ExceptionValues>({ resolver: zodResolver(exceptionSchema) })
  React.useEffect(() => {
    if (!settings) return
    clearState()
    reset({
      receives_commission: settings.effective.receives_commission,
      auto_payout: settings.effective.auto_payout,
      rate: bpsToInput(settings.effective.rate_bps),
      weight: String(settings.effective.weight_milli / 1000).replace('.', ','),
      valid_from: '',
      reason: '',
    })
  }, [settings, reset, clearState])
  const onSubmit = (v: ExceptionValues) => {
    if (!settings) return
    const payload: Record<string, unknown> = {
      establishment_id: settings.establishment_id,
      receives_commission: v.receives_commission,
      auto_payout: v.auto_payout,
    }
    if (v.rate) payload.rate_bps = parsePercentToBps(v.rate)
    if (v.weight) payload.weight_milli = Math.round(Number(v.weight.replace(',', '.')) * 1000)
    if (v.valid_from) payload.valid_from = `${v.valid_from}T03:00:00Z`
    return submit('COMMISSION_EXCEPTION', payload, v.reason)
  }
  return (
    <Dialog open={!!settings} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        {chain.step ?? (
          <>
        <DialogHeader>
          <DialogTitle>Exceção de comissão — {establishmentName}</DialogTitle>
          <DialogDescription>Taxa e peso valem a partir da data informada (ou da aprovação). Só vale depois que {copy.afterGate}.</DialogDescription>
        </DialogHeader>
        <ApprovalErrorAlert error={error} />
        <form id="comm-exc" onSubmit={handleSubmit(onSubmit)} noValidate>
          <FieldGroup>
            <Field orientation="horizontal" className="min-h-11">
              <Controller control={control} name="receives_commission" render={({ field }) => <Switch checked={field.value} onCheckedChange={field.onChange} />} />
              <FieldLabel className="font-medium">Recebe comissão</FieldLabel>
            </Field>
            <Field orientation="horizontal" className="min-h-11">
              <Controller control={control} name="auto_payout" render={({ field }) => <Switch checked={field.value} onCheckedChange={field.onChange} />} />
              <FieldLabel className="font-medium">Pagamento automático no dia 1</FieldLabel>
            </Field>
            <div className="grid grid-cols-1 gap-5 sm:grid-cols-3">
              <Field invalid={!!errors.rate}>
                <FieldLabel>Taxa (%)</FieldLabel>
                <Input inputMode="decimal" {...register('rate')} />
                <FieldDescription>Ex.: 3, 5 ou 7.</FieldDescription>
                <FieldError>{errors.rate?.message}</FieldError>
              </Field>
              <Field invalid={!!errors.weight}>
                <FieldLabel>Peso</FieldLabel>
                <Input inputMode="decimal" {...register('weight')} />
                <FieldDescription>1 = padrão.</FieldDescription>
                <FieldError>{errors.weight?.message}</FieldError>
              </Field>
              <Field invalid={!!errors.valid_from}>
                <FieldLabel>Válido a partir de</FieldLabel>
                <Input type="date" {...register('valid_from')} />
                <FieldError>{errors.valid_from?.message}</FieldError>
              </Field>
            </div>
            <Field invalid={!!errors.reason}>
              <FieldLabel required>{copy.reasonLabel}</FieldLabel>
              <Textarea rows={3} {...register('reason')} />
              <FieldError>{errors.reason?.message}</FieldError>
            </Field>
          </FieldGroup>
        </form>
        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Cancelar
          </Button>
          <Button type="submit" form="comm-exc" loading={isSubmitting} loadingText="Enviando…">
            {copy.submitLabel}
          </Button>
        </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}

// ---------------------------------------------------------------------------
const adjustSchema = z.object({
  direction: z.enum(['credit', 'debit']),
  amount: z.string().trim().refine((v) => (parseBRLToCents(v) ?? 0) > 0, 'Valor inválido.'),
  description: z.string().trim().min(3, 'Descreva o ajuste.').max(300),
  reason,
})
type AdjustValues = z.infer<typeof adjustSchema>

export function CommissionAdjustmentDialog({ establishment, onOpenChange }: { establishment: { id: string; name: string } | null; onOpenChange: (o: boolean) => void }) {
  const { submit, error, clearState, chain, copy } = useSubmitApproval(() => onOpenChange(false))
  const { register, control, handleSubmit, reset, formState: { errors, isSubmitting } } = useForm<AdjustValues>({ resolver: zodResolver(adjustSchema) })
  React.useEffect(() => {
    if (establishment) {
      clearState()
      reset({ direction: 'credit', amount: '', description: '', reason: '' })
    }
  }, [establishment, reset, clearState])
  const onSubmit = (v: AdjustValues) => {
    if (!establishment) return
    const cents = parseBRLToCents(v.amount) as number
    return submit('COMMISSION_ADJUSTMENT', { establishment_id: establishment.id, amount_cents: v.direction === 'credit' ? cents : -cents, description: v.description }, v.reason)
  }
  return (
    <Dialog open={!!establishment} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        {chain.step ?? (
          <>
        <DialogHeader>
          <DialogTitle>Ajuste manual de comissão — {establishment?.name}</DialogTitle>
          <DialogDescription>Crédito ou débito auditado no saldo do estabelecimento, depois que {copy.afterGate}.</DialogDescription>
        </DialogHeader>
        <ApprovalErrorAlert error={error} />
        <form id="comm-adj" onSubmit={handleSubmit(onSubmit)} noValidate>
          <FieldGroup>
            <Field>
              <FieldLabel id="adj-dir">Tipo</FieldLabel>
              <Controller
                control={control}
                name="direction"
                render={({ field }) => (
                  <ToggleGroup type="single" value={field.value} onValueChange={(v) => v && field.onChange(v)} aria-labelledby="adj-dir" className="self-start">
                    <ToggleGroupItem value="credit">Crédito (+)</ToggleGroupItem>
                    <ToggleGroupItem value="debit">Débito (−)</ToggleGroupItem>
                  </ToggleGroup>
                )}
              />
            </Field>
            <Field invalid={!!errors.amount}>
              <FieldLabel required>Valor (R$)</FieldLabel>
              <Input inputMode="decimal" className="max-w-[200px]" {...register('amount')} />
              <FieldError>{errors.amount?.message}</FieldError>
            </Field>
            <Field invalid={!!errors.description}>
              <FieldLabel required>Descrição</FieldLabel>
              <Input {...register('description')} />
              <FieldError>{errors.description?.message}</FieldError>
            </Field>
            <Field invalid={!!errors.reason}>
              <FieldLabel required>{copy.reasonLabel}</FieldLabel>
              <Textarea rows={3} {...register('reason')} />
              <FieldError>{errors.reason?.message}</FieldError>
            </Field>
          </FieldGroup>
        </form>
        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Cancelar
          </Button>
          <Button type="submit" form="comm-adj" loading={isSubmitting} loadingText="Enviando…">
            {copy.submitLabel}
          </Button>
        </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
