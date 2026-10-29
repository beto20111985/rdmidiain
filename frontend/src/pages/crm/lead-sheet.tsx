/** Detalhe do lead: dados, etapa (com histórico), atividades, propostas e edição. */
import * as React from 'react'
import { Link } from 'react-router-dom'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { FileText, Pencil } from 'lucide-react'
import { Sheet, SheetBody, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Separator } from '@/components/ui/separator'
import { Skeleton } from '@/components/ui/skeleton'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Field, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ErrorState } from '@/components/patterns/states'
import { Can } from '@/components/auth/guards'
import { DetailList } from '@/components/finance/detail-list'
import { StatusPill } from '@/components/finance/status-pill'
import { ACTIVITY_KINDS, LEAD_STAGE_STATUS, PROPOSAL_STATUS, leadStageLabel } from '@/components/finance/labels'
import { centsToInput, parseBRLToCents } from '@/components/finance/money'
import { qk } from '@/lib/queryKeys'
import { errorMessage } from '@/lib/errors'
import { EMPTY_VALUE, formatCents, formatDateTime } from '@/lib/format'
import { personLabel } from '@/components/finance/names'
import { useAuthStore } from '@/stores/authStore'
import { crmApi, type Lead, type LeadDetail } from './api'
import { StageChangeDialog } from './lead-dialogs'

const INTEREST: Record<string, string> = { ANUNCIANTE: 'Anunciar', PARCEIRO: 'Ser parceiro', AMBOS: 'Ambos' }

function ActivityForm({ leadId }: { leadId: string }) {
  const qc = useQueryClient()
  const [kind, setKind] = React.useState('NOTA')
  const [text, setText] = React.useState('')
  const [error, setError] = React.useState<string | null>(null)
  const add = useMutation({
    mutationFn: () => crmApi.addActivity(leadId, { kind, description: text.trim() }),
    onSuccess: async () => {
      setText('')
      toast.success('Atividade registrada.')
      await qc.invalidateQueries({ queryKey: ['crm'] })
    },
    onError: (e) => setError(errorMessage(e)),
  })
  return (
    <form
      className="flex flex-col gap-3"
      onSubmit={(e) => {
        e.preventDefault()
        if (!text.trim()) {
          setError('Descreva a atividade.')
          return
        }
        setError(null)
        add.mutate()
      }}
      noValidate
    >
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-[160px_1fr]">
        <Field>
          <FieldLabel>Tipo</FieldLabel>
          <Select value={kind} onValueChange={setKind}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                {ACTIVITY_KINDS.map((k) => (
                  <SelectItem key={k.value} value={k.value}>
                    {k.label}
                  </SelectItem>
                ))}
              </SelectGroup>
            </SelectContent>
          </Select>
        </Field>
        <Field invalid={!!error}>
          <FieldLabel required>Descrição</FieldLabel>
          <Textarea rows={2} value={text} onChange={(e) => setText(e.target.value)} maxLength={5000} />
          <FieldError>{error}</FieldError>
        </Field>
      </div>
      <Button type="submit" variant="secondary" loading={add.isPending} loadingText="Registrando…" className="self-start">
        Registrar atividade
      </Button>
    </form>
  )
}

const editSchema = z.object({
  name: z.string().trim().min(2, 'Informe o nome.').max(255),
  company: z.string().max(255).optional(),
  email: z.union([z.literal(''), z.string().trim().email('E-mail inválido.')]).optional(),
  phone: z.string().max(30).optional(),
  value: z.string().refine((v) => !v || parseBRLToCents(v) !== null, 'Valor inválido.').optional(),
  notes: z.string().max(5000).optional(),
})
type EditValues = z.infer<typeof editSchema>

function EditLead({ lead, onDone }: { lead: LeadDetail; onDone: () => void }) {
  const qc = useQueryClient()
  const [error, setError] = React.useState<string | null>(null)
  const { register, handleSubmit, formState: { errors, isSubmitting } } = useForm<EditValues>({
    resolver: zodResolver(editSchema),
    defaultValues: {
      name: lead.name,
      company: lead.company ?? '',
      email: lead.email ?? '',
      phone: lead.phone ?? '',
      value: centsToInput(lead.estimated_value_cents),
      notes: lead.notes ?? '',
    },
  })
  const onSubmit = async (v: EditValues) => {
    setError(null)
    try {
      await crmApi.patchLead(lead.id, {
        name: v.name,
        company: v.company || null,
        email: v.email || null,
        phone: v.phone || null,
        estimated_value_cents: v.value ? parseBRLToCents(v.value) : null,
        notes: v.notes || null,
      })
      toast.success('Lead atualizado.')
      await qc.invalidateQueries({ queryKey: ['crm'] })
      onDone()
    } catch (e) {
      setError(errorMessage(e))
    }
  }
  return (
    <form onSubmit={handleSubmit(onSubmit)} noValidate className="flex flex-col gap-4" aria-label="Editar lead">
      {error ? (
        <Alert variant="danger">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}
      <FieldGroup>
        <Field invalid={!!errors.name}>
          <FieldLabel required>Nome</FieldLabel>
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
        <Field invalid={!!errors.value}>
          <FieldLabel>Valor estimado (R$)</FieldLabel>
          <Input inputMode="decimal" {...register('value')} />
          <FieldError>{errors.value?.message}</FieldError>
        </Field>
        <Field>
          <FieldLabel>Observações</FieldLabel>
          <Textarea rows={3} {...register('notes')} />
        </Field>
      </FieldGroup>
      <div className="flex gap-3">
        <Button type="submit" loading={isSubmitting} loadingText="Salvando…">
          Salvar
        </Button>
        <Button type="button" variant="secondary" onClick={onDone}>
          Cancelar
        </Button>
      </div>
    </form>
  )
}

export function LeadSheet({ leadId, onOpenChange }: { leadId: string | null; onOpenChange: (o: boolean) => void }) {
  const meId = useAuthStore((s) => s.me?.user.id)
  const q = useQuery({ queryKey: qk.detail('crm', `lead:${leadId ?? ''}`), queryFn: () => crmApi.lead(leadId as string), enabled: !!leadId })
  const [stageFor, setStageFor] = React.useState<Lead | null>(null)
  const [editing, setEditing] = React.useState(false)
  React.useEffect(() => setEditing(false), [leadId])
  const l = q.data
  return (
    <Sheet open={!!leadId} onOpenChange={onOpenChange}>
      <SheetContent className="sm:max-w-[560px]">
        <SheetHeader>
          <SheetTitle>{l?.name ?? 'Lead'}</SheetTitle>
          <SheetDescription>{l?.company ?? 'Contato comercial'}</SheetDescription>
        </SheetHeader>
        <SheetBody className="flex flex-col gap-5 pb-6">
          {q.isLoading ? (
            <Skeleton className="h-48 w-full" />
          ) : q.error ? (
            <ErrorState error={q.error} onRetry={() => void q.refetch()} size="compact" />
          ) : l ? (
            editing ? (
              <EditLead lead={l} onDone={() => setEditing(false)} />
            ) : (
              <>
                <div className="flex flex-wrap items-center gap-2">
                  <StatusPill map={LEAD_STAGE_STATUS} value={l.stage} />
                  <Can permission="crm.manage">
                    <Button variant="secondary" onClick={() => setStageFor(l)}>
                      Mudar etapa
                    </Button>
                    <Button variant="ghost" onClick={() => setEditing(true)}>
                      <Pencil aria-hidden="true" />
                      Editar
                    </Button>
                    <Button asChild variant="ghost">
                      <Link to={`/crm/propostas/nova?lead=${l.id}`}>
                        <FileText aria-hidden="true" />
                        Nova proposta
                      </Link>
                    </Button>
                  </Can>
                </div>
                <DetailList
                  items={[
                    { label: 'E-mail', value: l.email ?? EMPTY_VALUE },
                    { label: 'Telefone', value: l.phone ?? EMPTY_VALUE },
                    { label: 'Interesse', value: l.interest ? INTEREST[l.interest] : EMPTY_VALUE },
                    { label: 'Origem', value: l.source },
                    { label: 'Cidade', value: l.city ? `${l.city}/${l.uf ?? ''}` : EMPTY_VALUE },
                    { label: 'Responsável', value: personLabel(l.owner_name, l.owner_id, meId) },
                    { label: 'Valor estimado', value: formatCents(l.estimated_value_cents), numeric: true },
                    { label: 'Na etapa desde', value: formatDateTime(l.stage_changed_at) },
                    ...(l.lost_reason ? [{ label: 'Motivo da perda', value: l.lost_reason, wide: true }] : []),
                    ...(l.notes ? [{ label: 'Observações', value: l.notes, wide: true }] : []),
                  ]}
                />
                <Separator />
                <section aria-labelledby="lead-props" className="flex flex-col gap-2">
                  <h3 id="lead-props" className="text-title-card text-text">
                    Propostas
                  </h3>
                  {l.proposals.length ? (
                    <ul className="m-0 flex list-none flex-col gap-2 p-0">
                      {l.proposals.map((p) => (
                        <li key={p.id} className="flex flex-wrap items-center justify-between gap-2">
                          <Link to={`/crm/propostas/${p.id}`} className="font-semibold text-link underline-offset-4 hover:underline">
                            {p.number}
                          </Link>
                          <span className="flex items-center gap-2">
                            <span className="tabular-nums">{formatCents(p.total_cents)}</span>
                            <StatusPill map={PROPOSAL_STATUS} value={p.status} />
                          </span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="m-0 text-sm text-text-muted">Nenhuma proposta para este lead.</p>
                  )}
                </section>
                <Separator />
                <section aria-labelledby="lead-acts" className="flex flex-col gap-3">
                  <h3 id="lead-acts" className="text-title-card text-text">
                    Atividades
                  </h3>
                  <Can permission="crm.manage">
                    <ActivityForm leadId={l.id} />
                  </Can>
                  {l.activities.length ? (
                    <ol className="m-0 flex list-none flex-col gap-3 p-0">
                      {l.activities.map((a) => (
                        <li key={a.id} className="flex flex-col gap-0.5 text-sm">
                          <span className="text-xs text-text-muted">
                            {ACTIVITY_KINDS.find((k) => k.value === a.kind)?.label ?? a.kind} · {formatDateTime(a.occurred_at)} · {personLabel(a.actor_name, a.actor_id, meId)}
                          </span>
                          <span className="whitespace-pre-wrap">{a.description}</span>
                        </li>
                      ))}
                    </ol>
                  ) : (
                    <p className="m-0 text-sm text-text-muted">Nenhuma atividade registrada.</p>
                  )}
                </section>
                <Separator />
                <section aria-labelledby="lead-hist" className="flex flex-col gap-2">
                  <h3 id="lead-hist" className="text-title-card text-text">
                    Histórico de etapas
                  </h3>
                  <ol className="m-0 flex list-none flex-col gap-2 p-0 text-sm">
                    {l.stage_history.map((h, i) => (
                      <li key={`${h.at}-${i}`} className="flex flex-col">
                        <span>
                          {h.from ? `${leadStageLabel(h.from)} → ` : 'Criado em '}
                          {leadStageLabel(h.to)}
                        </span>
                        <span className="text-xs text-text-muted">
                          {formatDateTime(h.at)} · {h.actor_id ? personLabel(h.actor_name, h.actor_id, meId) : 'sistema'}
                          {h.reason ? ` · ${h.reason}` : ''}
                        </span>
                      </li>
                    ))}
                  </ol>
                </section>
              </>
            )
          ) : null}
        </SheetBody>
        <StageChangeDialog lead={stageFor} onOpenChange={(o) => !o && setStageFor(null)} />
      </SheetContent>
    </Sheet>
  )
}
