/** Diálogos do CRM: novo lead e mudança de etapa (com histórico no servidor; motivo obrigatório para "Perdido"). */
import * as React from 'react'
import { Controller, useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { LEAD_TRANSITIONS, leadStageLabel } from '@/components/finance/labels'
import { parseBRLToCents } from '@/components/finance/money'
import { errorMessage, isApiError } from '@/lib/errors'
import { crmApi, type Lead } from './api'

const UF = ['AC', 'AL', 'AP', 'AM', 'BA', 'CE', 'DF', 'ES', 'GO', 'MA', 'MT', 'MS', 'MG', 'PA', 'PB', 'PR', 'PE', 'PI', 'RJ', 'RN', 'RS', 'RO', 'RR', 'SC', 'SP', 'SE', 'TO']

const leadSchema = z.object({
  name: z.string().trim().min(2, 'Informe o nome do contato.').max(255),
  company: z.string().trim().max(255).optional(),
  email: z.union([z.literal(''), z.string().trim().email('E-mail inválido.')]).optional(),
  phone: z.string().trim().max(30).optional(),
  interest: z.string().optional(),
  source: z.string().trim().max(50).optional(),
  city: z.string().trim().max(100).optional(),
  uf: z.string().optional(),
  value: z.string().trim().refine((v) => !v || parseBRLToCents(v) !== null, 'Valor inválido.').optional(),
  notes: z.string().max(5000).optional(),
})
type LeadValues = z.infer<typeof leadSchema>

export function NewLeadDialog({ open, onOpenChange, onCreated }: { open: boolean; onOpenChange: (o: boolean) => void; onCreated?: (l: Lead) => void }) {
  const qc = useQueryClient()
  const [error, setError] = React.useState<string | null>(null)
  const { register, control, handleSubmit, reset, setError: setFieldError, formState: { errors, isSubmitting } } = useForm<LeadValues>({ resolver: zodResolver(leadSchema) })
  React.useEffect(() => {
    if (open) {
      setError(null)
      reset({ name: '', company: '', email: '', phone: '', interest: undefined, source: 'manual', city: '', uf: undefined, value: '', notes: '' })
    }
  }, [open, reset])
  const onSubmit = async (v: LeadValues) => {
    setError(null)
    try {
      const l = await crmApi.createLead({
        name: v.name,
        company: v.company || undefined,
        email: v.email || undefined,
        phone: v.phone || undefined,
        interest: v.interest || undefined,
        source: v.source || 'manual',
        city: v.city || undefined,
        uf: v.uf || undefined,
        estimated_value_cents: v.value ? parseBRLToCents(v.value) : undefined,
        notes: v.notes || undefined,
      })
      toast.success('Lead cadastrado.')
      await qc.invalidateQueries({ queryKey: ['crm'] })
      onCreated?.(l)
      onOpenChange(false)
    } catch (e) {
      if (isApiError(e)) {
        const fe = e.fieldErrors
        let mapped = false
        for (const k of ['name', 'company', 'email', 'phone', 'city', 'uf', 'notes'] as const) {
          if (fe[k]) {
            setFieldError(k, { message: fe[k] })
            mapped = true
          }
        }
        if (!mapped) setError(e.message)
      } else setError(errorMessage(e))
    }
  }
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>Novo lead</DialogTitle>
          <DialogDescription>Leads com cidade/UF ficam no escopo regional; você é o responsável por padrão.</DialogDescription>
        </DialogHeader>
        {error ? (
          <Alert variant="danger">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        ) : null}
        <form id="lead-form" onSubmit={handleSubmit(onSubmit)} noValidate>
          <FieldGroup>
            <div className="grid grid-cols-1 gap-5 sm:grid-cols-2">
              <Field invalid={!!errors.name}>
                <FieldLabel required>Nome do contato</FieldLabel>
                <Input {...register('name')} />
                <FieldError>{errors.name?.message}</FieldError>
              </Field>
              <Field>
                <FieldLabel>Empresa</FieldLabel>
                <Input {...register('company')} />
              </Field>
              <Field invalid={!!errors.email}>
                <FieldLabel>E-mail</FieldLabel>
                <Input type="email" {...register('email')} />
                <FieldError>{errors.email?.message}</FieldError>
              </Field>
              <Field>
                <FieldLabel>Telefone</FieldLabel>
                <Input type="tel" {...register('phone')} />
              </Field>
              <Field>
                <FieldLabel>Interesse</FieldLabel>
                <Controller
                  control={control}
                  name="interest"
                  render={({ field }) => (
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger>
                        <SelectValue placeholder="Selecione" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectGroup>
                          <SelectItem value="ANUNCIANTE">Anunciar</SelectItem>
                          <SelectItem value="PARCEIRO">Ser parceiro (estabelecimento)</SelectItem>
                          <SelectItem value="AMBOS">Ambos</SelectItem>
                        </SelectGroup>
                      </SelectContent>
                    </Select>
                  )}
                />
              </Field>
              <Field>
                <FieldLabel>Origem</FieldLabel>
                <Input {...register('source')} placeholder="manual, indicação, site…" />
              </Field>
              <Field>
                <FieldLabel>Cidade</FieldLabel>
                <Input {...register('city')} />
              </Field>
              <Field>
                <FieldLabel>UF</FieldLabel>
                <Controller
                  control={control}
                  name="uf"
                  render={({ field }) => (
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger>
                        <SelectValue placeholder="UF" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectGroup>
                          {UF.map((u) => (
                            <SelectItem key={u} value={u}>
                              {u}
                            </SelectItem>
                          ))}
                        </SelectGroup>
                      </SelectContent>
                    </Select>
                  )}
                />
              </Field>
              <Field invalid={!!errors.value}>
                <FieldLabel>Valor estimado (R$)</FieldLabel>
                <Input inputMode="decimal" {...register('value')} />
                <FieldError>{errors.value?.message}</FieldError>
              </Field>
            </div>
            <Field>
              <FieldLabel>Observações</FieldLabel>
              <Textarea rows={3} {...register('notes')} />
            </Field>
          </FieldGroup>
        </form>
        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Cancelar
          </Button>
          <Button type="submit" form="lead-form" loading={isSubmitting} loadingText="Salvando…">
            Cadastrar lead
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ---------------------------------------------------------------------------
export function StageChangeDialog({ lead, onOpenChange, initialStage }: { lead: Lead | null; onOpenChange: (o: boolean) => void; initialStage?: string }) {
  const qc = useQueryClient()
  const [stage, setStage] = React.useState<string | undefined>(undefined)
  const [reason, setReason] = React.useState('')
  const [error, setError] = React.useState<string | null>(null)
  const [reasonError, setReasonError] = React.useState<string | null>(null)
  const [pending, setPending] = React.useState(false)
  const allowed = lead ? (LEAD_TRANSITIONS[lead.stage] ?? []) : []

  React.useEffect(() => {
    if (lead) {
      const next = LEAD_TRANSITIONS[lead.stage] ?? []
      setStage(initialStage && next.includes(initialStage) ? initialStage : undefined)
      setReason('')
      setError(null)
      setReasonError(null)
    }
  }, [lead, initialStage])

  const save = async () => {
    if (!lead || !stage) {
      setError('Escolha a nova etapa.')
      return
    }
    if (stage === 'PERDIDO' && reason.trim().length < 3) {
      setReasonError('Informe o motivo da perda.')
      return
    }
    setPending(true)
    setError(null)
    try {
      await crmApi.changeStage(lead.id, stage, reason.trim() || undefined)
      toast.success(`${lead.name}: ${leadStageLabel(lead.stage)} → ${leadStageLabel(stage)}.`)
      await qc.invalidateQueries({ queryKey: ['crm'] })
      onOpenChange(false)
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setPending(false)
    }
  }

  return (
    <Dialog open={!!lead} onOpenChange={(o) => (!pending ? onOpenChange(o) : undefined)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Mudar etapa — {lead?.name}</DialogTitle>
          <DialogDescription>Etapa atual: {leadStageLabel(lead?.stage)}. A mudança entra no histórico do lead.</DialogDescription>
        </DialogHeader>
        {error ? (
          <Alert variant="danger">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        ) : null}
        {allowed.length === 0 ? (
          <p className="m-0 text-sm text-text-muted">Este lead está em uma etapa final ({leadStageLabel(lead?.stage)}).</p>
        ) : (
          <FieldGroup>
            <Field>
              <FieldLabel required>Nova etapa</FieldLabel>
              <Select value={stage} onValueChange={setStage}>
                <SelectTrigger>
                  <SelectValue placeholder="Selecione" />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    {allowed.map((s) => (
                      <SelectItem key={s} value={s}>
                        {leadStageLabel(s)}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
              <FieldDescription>Só aparecem as etapas permitidas a partir da atual.</FieldDescription>
            </Field>
            <Field invalid={!!reasonError}>
              <FieldLabel required={stage === 'PERDIDO'}>Motivo</FieldLabel>
              <Textarea rows={2} value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} />
              <FieldError>{reasonError}</FieldError>
            </Field>
          </FieldGroup>
        )}
        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)} disabled={pending}>
            Cancelar
          </Button>
          {allowed.length ? (
            <Button onClick={() => void save()} loading={pending} loadingText="Salvando…">
              Mudar etapa
            </Button>
          ) : null}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
