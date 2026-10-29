/**
 * Formulários de despesa/conta a pagar. Criar, alterar e baixar NÃO lançam nada direto:
 * cada envio cria um PEDIDO de aprovação (P001 §85, §91). Na DUPLA só executa quando o outro Admin
 * Master aprovar; na ÚNICA o próprio Master autoriza no mesmo diálogo (AÇÃO 003).
 */
import * as React from 'react'
import { useForm, Controller } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { useQueryClient } from '@tanstack/react-query'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel, FieldLegend, FieldSet } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Button } from '@/components/ui/button'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { centsToInput, parseBRLToCents } from '@/components/finance/money'
import { PIX_KEY_TYPES } from '@/components/finance/labels'
import { approvalCopy, toastApprovalCreated, useInstanceApprovalMode } from '@/components/finance/approval-utils'
import { ApprovalErrorAlert } from '@/components/finance/approval-errors'
import { useAuthorizationChain } from '@/components/finance/authorize-chain'
import { isIsoDate } from '@/components/finance/period'
import { todayIn } from '@/lib/dates'
import { isApiError } from '@/lib/errors'
import { pixKeyFieldError } from '@/components/finance/payment-errors'
import { formatCents } from '@/lib/format'
import { newRequestId } from '@/lib/http'
import { financeApi, type Payable } from './api'

const reasonSchema = z.string().trim().min(10, 'Explique o motivo (mínimo 10 caracteres).').max(2000)
const moneySchema = z
  .string()
  .trim()
  .min(1, 'Informe o valor.')
  .refine((v) => (parseBRLToCents(v) ?? 0) > 0, 'Valor inválido. Use o formato 1.234,56.')

const expenseSchema = z.object({
  description: z.string().trim().min(3, 'Descreva a despesa.').max(300),
  category: z
    .string()
    .trim()
    .min(2, 'Informe a categoria.')
    .max(40)
    .regex(/^[\p{L}\d _-]+$/u, 'Use letras, números, espaço, hífen ou sublinhado.'),
  amount: moneySchema,
  competence_date: z.string().refine(isIsoDate, 'Informe a data de competência.'),
  due_date: z.string().refine((v) => !v || isIsoDate(v), 'Data inválida.'),
  supplier_name: z.string().max(200).optional(),
  supplier_document: z.string().max(20).optional(),
  reason: reasonSchema,
})
type ExpenseValues = z.infer<typeof expenseSchema>

export function ExpenseDialog({ open, onOpenChange, payable }: { open: boolean; onOpenChange: (o: boolean) => void; payable?: Payable | null }) {
  const qc = useQueryClient()
  const editing = !!payable
  const [serverError, setServerError] = React.useState<unknown>(null)
  const idem = React.useRef<string | null>(null)
  const chain = useAuthorizationChain(() => onOpenChange(false))
  const copy = approvalCopy(useInstanceApprovalMode().mode)
  const {
    register,
    handleSubmit,
    reset,
    setError,
    formState: { errors, isSubmitting },
  } = useForm<ExpenseValues>({ resolver: zodResolver(expenseSchema) })

  React.useEffect(() => {
    if (!open) return
    setServerError(null)
    idem.current = null
    chain.reset()
    reset({
      description: payable?.description ?? '',
      category: payable?.category ?? '',
      amount: payable ? centsToInput(payable.amount_cents) : '',
      competence_date: payable?.competence_date ?? todayIn(),
      due_date: payable?.due_date ?? '',
      supplier_name: payable?.supplier_name ?? '',
      supplier_document: '',
      reason: '',
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, payable, reset])

  const onSubmit = async (v: ExpenseValues) => {
    setServerError(null)
    const amount_cents = parseBRLToCents(v.amount) as number
    try {
      const r = editing
        ? await financeApi.updateExpense(payable!.id, {
            description: v.description,
            category: v.category,
            amount_cents,
            due_date: v.due_date || undefined,
            reason: v.reason,
          })
        : await financeApi.createExpense(
            {
              description: v.description,
              category: v.category,
              amount_cents,
              competence_date: v.competence_date,
              due_date: v.due_date || undefined,
              supplier_name: v.supplier_name || undefined,
              supplier_document: v.supplier_document || undefined,
              reason: v.reason,
            },
            (idem.current ??= newRequestId()),
          )
      await qc.invalidateQueries({ queryKey: ['finance'] })
      if (chain.handleCreated(r)) return
      toastApprovalCreated(r)
      onOpenChange(false)
    } catch (e) {
      if (isApiError(e)) {
        const fe = e.fieldErrors
        let mapped = false
        for (const k of ['description', 'category', 'competence_date', 'due_date', 'reason'] as const) {
          if (fe[k]) {
            setError(k, { message: fe[k] })
            mapped = true
          }
        }
        if (fe.amount_cents || e.code === 'EXPENSE_AMOUNT_INVALID') {
          setError('amount', { message: fe.amount_cents ?? e.message })
          mapped = true
        }
        if (e.code === 'EXPENSE_CATEGORY_INVALID') {
          setError('category', { message: e.message })
          mapped = true
        }
        if (!mapped) setServerError(e)
      } else setServerError(e)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        {chain.step ?? (
          <>
        <DialogHeader>
          <DialogTitle>{editing ? 'Alterar despesa' : 'Nova despesa'}</DialogTitle>
          <DialogDescription>
            O lançamento só acontece depois que {copy.afterGate}.
          </DialogDescription>
        </DialogHeader>
        <ApprovalErrorAlert error={serverError} />
        <form id="expense-form" onSubmit={handleSubmit(onSubmit)} noValidate>
          <FieldGroup>
            <Field invalid={!!errors.description}>
              <FieldLabel required>Descrição</FieldLabel>
              <Input {...register('description')} />
              <FieldError>{errors.description?.message}</FieldError>
            </Field>
            <div className="grid grid-cols-1 gap-5 sm:grid-cols-2">
              <Field invalid={!!errors.category}>
                <FieldLabel required>Categoria</FieldLabel>
                <Input {...register('category')} placeholder="ex.: internet, aluguel" />
                <FieldError>{errors.category?.message}</FieldError>
              </Field>
              <Field invalid={!!errors.amount}>
                <FieldLabel required>Valor (R$)</FieldLabel>
                <Input inputMode="decimal" placeholder="0,00" {...register('amount')} />
                <FieldError>{errors.amount?.message}</FieldError>
              </Field>
              <Field invalid={!!errors.competence_date} disabled={editing}>
                <FieldLabel required>Competência</FieldLabel>
                <Input type="date" {...register('competence_date')} />
                {editing ? <FieldDescription>A competência não é alterada.</FieldDescription> : null}
                <FieldError>{errors.competence_date?.message}</FieldError>
              </Field>
              <Field invalid={!!errors.due_date}>
                <FieldLabel>Vencimento</FieldLabel>
                <Input type="date" {...register('due_date')} />
                <FieldDescription>Em branco = data de competência.</FieldDescription>
                <FieldError>{errors.due_date?.message}</FieldError>
              </Field>
              {!editing ? (
                <>
                  <Field>
                    <FieldLabel>Fornecedor</FieldLabel>
                    <Input {...register('supplier_name')} />
                  </Field>
                  <Field>
                    <FieldLabel>CPF/CNPJ do fornecedor</FieldLabel>
                    <Input {...register('supplier_document')} inputMode="numeric" />
                  </Field>
                </>
              ) : null}
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
          <Button type="submit" form="expense-form" loading={isSubmitting} loadingText="Enviando…">
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
const settleSchema = z
  .object({
    method: z.enum(['EXTERNAL', 'PIX_OUT']),
    pix_key_type: z.string().optional(),
    pix_key: z.string().trim().max(120).optional(),
    reason: reasonSchema,
  })
  .superRefine((v, ctx) => {
    if (v.method === 'PIX_OUT') {
      if (!v.pix_key_type) ctx.addIssue({ code: 'custom', path: ['pix_key_type'], message: 'Escolha o tipo de chave.' })
      if (!v.pix_key || v.pix_key.length < 3) ctx.addIssue({ code: 'custom', path: ['pix_key'], message: 'Informe a chave PIX do favorecido.' })
    }
  })
type SettleValues = z.infer<typeof settleSchema>

export function SettleDialog({ payable, onOpenChange }: { payable: Payable | null; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient()
  const [serverError, setServerError] = React.useState<unknown>(null)
  const chain = useAuthorizationChain(() => onOpenChange(false))
  const instanceMode = useInstanceApprovalMode().mode
  const copy = approvalCopy(instanceMode)
  const {
    register,
    control,
    handleSubmit,
    reset,
    watch,
    setError,
    formState: { errors, isSubmitting },
  } = useForm<SettleValues>({ resolver: zodResolver(settleSchema), defaultValues: { method: 'EXTERNAL', reason: '' } })
  const method = watch('method')

  React.useEffect(() => {
    if (payable) {
      setServerError(null)
      chain.reset()
      reset({ method: 'EXTERNAL', pix_key_type: undefined, pix_key: '', reason: '' })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [payable, reset])

  const onSubmit = async (v: SettleValues) => {
    if (!payable) return
    setServerError(null)
    try {
      const r = await financeApi.settlePayable(payable.id, {
        method: v.method,
        pix_key_type: v.method === 'PIX_OUT' ? v.pix_key_type : undefined,
        pix_key: v.method === 'PIX_OUT' ? v.pix_key : undefined,
        reason: v.reason,
      })
      await qc.invalidateQueries({ queryKey: ['finance'] })
      if (chain.handleCreated(r)) return
      toastApprovalCreated(r)
      onOpenChange(false)
    } catch (e) {
      const keyErr = pixKeyFieldError(e)
      if (keyErr) setError('pix_key', { message: keyErr })
      else setServerError(e)
    }
  }

  return (
    <Dialog open={!!payable} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        {chain.step ?? (
          <>
        <DialogHeader>
          <DialogTitle>Registrar pagamento</DialogTitle>
          <DialogDescription>
            {payable ? `${payable.description} — ${formatCents(payable.amount_cents)}. ` : ''}
            {instanceMode === 'SINGLE'
              ? 'A baixa só é feita depois que você autorizar o pedido, com autenticador, senha financeira e código por e-mail.'
              : 'A baixa só é feita após a aprovação do outro Admin Master. PIX OUT exige também o código enviado por e-mail ao aprovador.'}
          </DialogDescription>
        </DialogHeader>
        <ApprovalErrorAlert error={serverError} />
        <form id="settle-form" onSubmit={handleSubmit(onSubmit)} noValidate>
          <FieldGroup>
            <FieldSet>
              <FieldLegend>Forma de pagamento</FieldLegend>
              <Controller
                control={control}
                name="method"
                render={({ field }) => (
                  <RadioGroup value={field.value} onValueChange={field.onChange} className="flex flex-col gap-2">
                    <Field orientation="horizontal" className="min-h-11 flex-nowrap">
                      <RadioGroupItem value="EXTERNAL" />
                      <FieldLabel className="min-w-0 flex-1 font-medium">Pago fora da plataforma (conta bancária externa)</FieldLabel>
                    </Field>
                    <Field orientation="horizontal" className="min-h-11 flex-nowrap">
                      <RadioGroupItem value="PIX_OUT" />
                      <FieldLabel className="min-w-0 flex-1 font-medium">PIX OUT pela CloudPayPag</FieldLabel>
                    </Field>
                  </RadioGroup>
                )}
              />
            </FieldSet>
            {method === 'PIX_OUT' ? (
              <div className="grid grid-cols-1 gap-5 sm:grid-cols-2">
                <Field invalid={!!errors.pix_key_type}>
                  <FieldLabel required>Tipo de chave</FieldLabel>
                  <Controller
                    control={control}
                    name="pix_key_type"
                    render={({ field }) => (
                      <Select value={field.value} onValueChange={field.onChange}>
                        <SelectTrigger>
                          <SelectValue placeholder="Selecione" />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectGroup>
                            {PIX_KEY_TYPES.map((t) => (
                              <SelectItem key={t.value} value={t.value}>
                                {t.label}
                              </SelectItem>
                            ))}
                          </SelectGroup>
                        </SelectContent>
                      </Select>
                    )}
                  />
                  <FieldError>{errors.pix_key_type?.message}</FieldError>
                </Field>
                <Field invalid={!!errors.pix_key}>
                  <FieldLabel required>Chave PIX do favorecido</FieldLabel>
                  <Input autoComplete="off" {...register('pix_key')} />
                  <FieldError>{errors.pix_key?.message}</FieldError>
                </Field>
              </div>
            ) : null}
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
          <Button type="submit" form="settle-form" loading={isSubmitting} loadingText="Enviando…">
            {copy.submitLabel}
          </Button>
        </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
