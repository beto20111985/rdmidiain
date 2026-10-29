/**
 * Formulário de proposta (rascunho): destinatário (lead ou organização), itens por pacote,
 * desconto em % (acima do limite da RD exige justificativa e aprovação de um gestor — o
 * vendedor não aprova o próprio), observações. Os totais OFICIAIS são calculados pelo servidor.
 */
import * as React from 'react'
import { Controller, useFieldArray, useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Plus, Trash2 } from 'lucide-react'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel, FieldLegend, FieldSet } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { bpsToInput, centsToInput, parseBRLToCents, parsePercentToBps } from '@/components/finance/money'
import { EntityPicker, type PickerOption } from '@/components/campaigns/entity-picker'
import { useCan } from '@/hooks/usePermissions'
import { http, type Paginated } from '@/lib/http'
import { qk } from '@/lib/queryKeys'
import { errorMessage, isApiError } from '@/lib/errors'
import { formatBps, formatCents } from '@/lib/format'
import { crmApi, type Proposal } from './api'
import { INSTANCE_CLIENT_MESSAGE, searchClients } from './client-options'

interface PlanLite {
  id: string
  name: string
  slug: string
  status: string
  reference_quote?: { total_cents?: number } | null
  reference_total_cents?: number | null
}

const itemSchema = z.object({
  package_code: z.string().max(60).optional(),
  description: z.string().trim().min(1, 'Descreva o item.').max(500),
  quantity: z.string().trim().regex(/^\d{1,6}$/, 'Quantidade inteira.').refine((v) => Number(v) >= 1, 'Mínimo 1.'),
  unit_price: z.string().trim().refine((v) => parseBRLToCents(v) !== null, 'Valor inválido.'),
})

const schema = z
  .object({
    title: z.string().trim().min(3, 'Informe o título (mínimo 3 caracteres).').max(255),
    lead_id: z.string().optional(),
    org_id: z.string().optional(),
    discount: z.string().trim().refine((v) => !v || parsePercentToBps(v) !== null, 'Percentual inválido (0 a 100).'),
    discount_justification: z.string().max(1000).optional(),
    notes: z.string().max(5000).optional(),
    items: z.array(itemSchema).min(1, 'Inclua ao menos um item.'),
  })
  .refine((v) => !!v.lead_id || !!v.org_id, { path: ['lead_id'], message: 'Escolha o lead ou a organização da proposta.' })
type Values = z.infer<typeof schema>

function toItems(v: Values) {
  return v.items.map((i) => ({
    package_code: i.package_code || undefined,
    description: i.description,
    quantity: Number(i.quantity),
    unit_price_cents: parseBRLToCents(i.unit_price) as number,
  }))
}

export function ProposalForm({ proposal, initialLeadId, onSaved }: { proposal?: Proposal | null; initialLeadId?: string | null; onSaved: (p: Proposal) => void }) {
  const qc = useQueryClient()
  const can = useCan()
  const [client, setClient] = React.useState<PickerOption | null>(
    proposal?.org_id ? { id: proposal.org_id, label: proposal.org_name ?? `Empresa ${proposal.org_id.slice(0, 8)}` } : null,
  )
  const editing = !!proposal
  const [error, setError] = React.useState<string | null>(null)
  const leads = useQuery({ queryKey: qk.list('crm', { kind: 'leads', page_size: 100, picker: true }), queryFn: () => crmApi.leads({ page_size: 100 }) })
  const plans = useQuery({
    queryKey: qk.list('pricing', { kind: 'plans', page_size: 100 }),
    queryFn: () => http.get<Paginated<PlanLite>>('/pricing/plans', { query: { page_size: 100 } }),
    enabled: can('pricing.view'),
    retry: false,
  })
  const { register, control, handleSubmit, watch, setValue, setError: setFieldError, formState: { errors, isSubmitting } } = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: {
      title: proposal?.title ?? '',
      lead_id: proposal?.lead_id ?? initialLeadId ?? undefined,
      org_id: proposal?.org_id ?? undefined,
      discount: proposal ? bpsToInput(proposal.discount_bps) : '',
      discount_justification: proposal?.discount_justification ?? '',
      notes: proposal?.notes ?? '',
      items: proposal?.items?.length
        ? proposal.items.map((i) => ({ package_code: i.package_code ?? '', description: i.description, quantity: String(i.quantity), unit_price: centsToInput(i.unit_price_cents) }))
        : [{ package_code: '', description: '', quantity: '1', unit_price: '' }],
    },
  })
  const items = useFieldArray({ control, name: 'items' })
  const watched = watch('items')
  const discountBps = parsePercentToBps(watch('discount') || '0') ?? 0
  const subtotal = (watched ?? []).reduce((a, i) => a + (Number(i.quantity) || 0) * (parseBRLToCents(i.unit_price) ?? 0), 0)
  const discount = Math.floor((subtotal * discountBps) / 10000)

  const applyPlan = (index: number, planId: string) => {
    const p = plans.data?.items.find((x) => x.id === planId)
    if (!p) return
    setValue(`items.${index}.package_code`, p.slug)
    setValue(`items.${index}.description`, p.name)
    const ref = p.reference_quote?.total_cents ?? p.reference_total_cents
    if (typeof ref === 'number') setValue(`items.${index}.unit_price`, centsToInput(ref))
  }

  const onSubmit = async (v: Values) => {
    setError(null)
    const body = {
      title: v.title,
      org_id: v.org_id || undefined,
      discount_bps: discountBps,
      discount_justification: v.discount_justification || undefined,
      notes: v.notes || undefined,
      items: toItems(v),
    }
    try {
      const p = editing ? await crmApi.patchProposal(proposal!.id, body) : await crmApi.createProposal({ ...body, lead_id: v.lead_id || undefined })
      toast.success(editing ? 'Proposta atualizada.' : `Proposta ${p.number} criada como rascunho.`)
      await qc.invalidateQueries({ queryKey: ['crm'] })
      onSaved(p)
    } catch (e) {
      if (isApiError(e)) {
        if (e.code === 'PROPOSAL_TARGET_REQUIRED') setFieldError('lead_id', { message: e.message })
        else if (e.code === 'CLIENT_IS_INSTANCE') setFieldError('org_id', { message: INSTANCE_CLIENT_MESSAGE })
        else setError(e.message)
      } else setError(errorMessage(e))
    }
  }

  return (
    <form onSubmit={handleSubmit(onSubmit)} noValidate className="flex flex-col gap-6" aria-label={editing ? 'Editar proposta' : 'Nova proposta'}>
      {error ? (
        <Alert variant="danger">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}
      <FieldGroup>
        <Field invalid={!!errors.title}>
          <FieldLabel required>Título</FieldLabel>
          <Input {...register('title')} placeholder="ex.: Campanha de lançamento — 20 telas em Goiânia" />
          <FieldError>{errors.title?.message}</FieldError>
        </Field>
        <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
          <Field invalid={!!errors.lead_id} disabled={editing}>
            <FieldLabel>Lead</FieldLabel>
            <Controller
              control={control}
              name="lead_id"
              render={({ field }) => (
                <Select value={field.value} onValueChange={field.onChange} disabled={editing}>
                  <SelectTrigger>
                    <SelectValue placeholder={leads.isLoading ? 'Carregando…' : 'Selecione o lead'} />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      {(leads.data?.items ?? []).map((l) => (
                        <SelectItem key={l.id} value={l.id}>
                          {l.name}
                          {l.company ? ` — ${l.company}` : ''}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  </SelectContent>
                </Select>
              )}
            />
            <FieldDescription>Ou escolha diretamente a organização cliente.</FieldDescription>
            <FieldError>{errors.lead_id?.message}</FieldError>
          </Field>
          <Controller
            control={control}
            name="org_id"
            render={({ field }) => (
              <EntityPicker
                label="Organização (cliente)"
                domain="crm"
                value={client}
                onChange={(o) => {
                  setClient(o)
                  field.onChange(o?.id ?? '')
                }}
                search={searchClients}
                placeholder="Busque por nome, nome fantasia ou CNPJ"
                description="Anunciantes e agências ativos. Obrigatória para converter em pedido."
                error={errors.org_id?.message}
              />
            )}
          />
        </div>
        <FieldSet>
          <FieldLegend>Itens (pacotes)</FieldLegend>
          {items.fields.map((f, i) => (
            <div key={f.id} className="flex flex-col gap-3 rounded-lg border border-border p-3">
              {plans.data?.items.length ? (
                <Field className="max-w-md">
                  <FieldLabel>Pacote do catálogo (opcional)</FieldLabel>
                  <Select onValueChange={(v) => applyPlan(i, v)}>
                    <SelectTrigger>
                      <SelectValue placeholder="Preencher a partir de um pacote" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectGroup>
                        {plans.data.items.map((p) => (
                          <SelectItem key={p.id} value={p.id}>
                            {p.name} ({p.status})
                          </SelectItem>
                        ))}
                      </SelectGroup>
                    </SelectContent>
                  </Select>
                </Field>
              ) : null}
              <div className="grid grid-cols-1 gap-3 md:grid-cols-[1fr_110px_160px_auto]">
                <Field invalid={!!errors.items?.[i]?.description}>
                  <FieldLabel required>Descrição</FieldLabel>
                  <Input {...register(`items.${i}.description`)} />
                  <FieldError>{errors.items?.[i]?.description?.message}</FieldError>
                </Field>
                <Field invalid={!!errors.items?.[i]?.quantity}>
                  <FieldLabel required>Qtd.</FieldLabel>
                  <Input inputMode="numeric" {...register(`items.${i}.quantity`)} />
                  <FieldError>{errors.items?.[i]?.quantity?.message}</FieldError>
                </Field>
                <Field invalid={!!errors.items?.[i]?.unit_price}>
                  <FieldLabel required>Valor unitário (R$)</FieldLabel>
                  <Input inputMode="decimal" {...register(`items.${i}.unit_price`)} />
                  <FieldError>{errors.items?.[i]?.unit_price?.message}</FieldError>
                </Field>
                <div className="flex items-end">
                  <Button type="button" variant="ghost" size="icon" aria-label={`Remover item ${i + 1}`} disabled={items.fields.length <= 1} onClick={() => items.remove(i)}>
                    <Trash2 aria-hidden="true" />
                  </Button>
                </div>
              </div>
              <Field className="max-w-xs">
                <FieldLabel>Código do pacote</FieldLabel>
                <Input {...register(`items.${i}.package_code`)} />
              </Field>
            </div>
          ))}
          {errors.items?.message ? (
            <p role="alert" className="m-0 text-xs font-medium text-danger">
              {errors.items.message}
            </p>
          ) : null}
          <Button type="button" variant="secondary" className="self-start" onClick={() => items.append({ package_code: '', description: '', quantity: '1', unit_price: '' })}>
            <Plus aria-hidden="true" />
            Adicionar item
          </Button>
        </FieldSet>
        <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
          <Field invalid={!!errors.discount}>
            <FieldLabel>Desconto (%)</FieldLabel>
            <Input inputMode="decimal" className="max-w-[160px]" {...register('discount')} />
            <FieldDescription>
              {proposal?.discount_limit_bps !== null && proposal?.discount_limit_bps !== undefined
                ? `Limite sem aprovação: ${formatBps(proposal.discount_limit_bps)}.`
                : 'Acima do limite definido pela RD, o desconto vai para aprovação de um gestor.'}
            </FieldDescription>
            <FieldError>{errors.discount?.message}</FieldError>
          </Field>
          <Field>
            <FieldLabel>Justificativa do desconto</FieldLabel>
            <Textarea rows={2} {...register('discount_justification')} />
            <FieldDescription>Obrigatória quando o desconto passa do limite.</FieldDescription>
          </Field>
        </div>
        <Field>
          <FieldLabel>Observações (saem no PDF)</FieldLabel>
          <Textarea rows={3} {...register('notes')} />
        </Field>
      </FieldGroup>
      <div role="status" className="flex flex-col gap-1 rounded-lg border border-border p-4 text-sm">
        <span className="text-xs font-semibold uppercase tracking-metric text-text-muted">Prévia a partir dos itens digitados</span>
        <span>
          Subtotal {formatCents(subtotal)} · desconto {formatCents(discount)} · total <strong className="tabular-nums">{formatCents(subtotal - discount)}</strong>
        </span>
        <span className="text-xs text-text-muted">O valor oficial é recalculado pelo servidor ao salvar; o preço final do pedido é recalculado no checkout.</span>
      </div>
      <Button type="submit" loading={isSubmitting} loadingText="Salvando…" className="self-start">
        {editing ? 'Salvar alterações' : 'Criar rascunho'}
      </Button>
    </form>
  )
}
