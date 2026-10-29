/**
 * Financeiro → Conciliação CloudPayPag (P001 §81–§82; ETAPA2 #652–#660; REQ-PAY-006).
 * Execuções (saldo do provedor × caixa interno; saques em processamento/desconhecidos),
 * "Reconsultar agora" onde a API permite (saldo e saques — cobranças NÃO têm consulta na API:
 * BLOQUEADO POR DEPENDÊNCIA EXTERNA), itens DIVERGENTES e resolução SOMENTE por ajuste
 * aprovado (pedido de aprovação RECONCILIATION_ADJUSTMENT: dupla, ou única com autorização no mesmo diálogo).
 */
import * as React from 'react'
import { useFieldArray, useForm, Controller } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { GitCompareArrows, Plus, RefreshCw, Trash2 } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel, FieldLegend, FieldSet } from '@/components/ui/field'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { Can } from '@/components/auth/guards'
import { StatusPill } from '@/components/finance/status-pill'
import { RECON_KIND_LABEL, RECON_RUN_STATUS, RECON_STATUS } from '@/components/finance/labels'
import { approvalCopy, useIsAdminMaster, toastApprovalCreated, useInstanceApprovalMode } from '@/components/finance/approval-utils'
import { ApprovalErrorAlert } from '@/components/finance/approval-errors'
import { useAuthorizationChain } from '@/components/finance/authorize-chain'
import { parseBRLToCents } from '@/components/finance/money'
import { isIsoDate } from '@/components/finance/period'
import { LiveStale } from '@/components/finance/live-stale'
import { useTableState } from '@/hooks/useTableState'
import { qk } from '@/lib/queryKeys'
import { errorMessage } from '@/lib/errors'
import { EMPTY_VALUE, formatCents, formatDateTime, formatNumber } from '@/lib/format'
import { financeApi, type ReconItem, type ReconRun } from './api'

const legSchema = z.object({
  account_code: z.string().trim().min(3, 'Informe o código da conta.'),
  owner_id: z.string().trim().optional(),
  amount: z.string().trim().refine((v) => {
    const neg = v.startsWith('-')
    const c = parseBRLToCents(neg ? v.slice(1) : v)
    return !!c && c > 0
  }, 'Valor inválido (use - para crédito).'),
  memo: z.string().max(200).optional(),
})

const resolutionSchema = z
  .object({
    mode: z.enum(['withdrawal', 'legs']),
    outcome: z.enum(['COMPLETED', 'FAILED']).optional(),
    external_id: z.string().max(80).optional(),
    fee: z.string().optional(),
    legs: z.array(legSchema),
    competence_date: z.string().optional(),
    description: z.string().max(300).optional(),
    reason: z.string().trim().min(10, 'Explique o motivo (mínimo 10 caracteres).').max(2000),
  })
  .superRefine((v, ctx) => {
    if (v.mode === 'withdrawal' && !v.outcome) ctx.addIssue({ code: 'custom', path: ['outcome'], message: 'Informe o desfecho conferido no painel do provedor.' })
    if (v.mode === 'legs') {
      if (v.legs.length < 2) ctx.addIssue({ code: 'custom', path: ['legs'], message: 'Informe ao menos duas partidas.' })
      const sum = v.legs.reduce((a, l) => {
        const neg = l.amount.startsWith('-')
        const c = parseBRLToCents(neg ? l.amount.slice(1) : l.amount) ?? 0
        return a + (neg ? -c : c)
      }, 0)
      if (v.legs.length >= 2 && sum !== 0) ctx.addIssue({ code: 'custom', path: ['legs'], message: `Débitos e créditos não fecham (diferença ${formatCents(sum)}).` })
    }
    if (v.competence_date && !isIsoDate(v.competence_date)) ctx.addIssue({ code: 'custom', path: ['competence_date'], message: 'Data inválida.' })
  })
type ResolutionValues = z.infer<typeof resolutionSchema>

function signedCents(v: string): number {
  const neg = v.trim().startsWith('-')
  const c = parseBRLToCents(neg ? v.trim().slice(1) : v.trim()) ?? 0
  return neg ? -c : c
}

function ResolutionDialog({ item, onOpenChange }: { item: ReconItem | null; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient()
  const [serverError, setServerError] = React.useState<unknown>(null)
  const chain = useAuthorizationChain(() => onOpenChange(false))
  const instanceMode = useInstanceApprovalMode().mode
  const copy = approvalCopy(instanceMode)
  const isWithdrawal = !!item && item.kind.startsWith('WITHDRAWAL_UNKNOWN')
  const {
    register,
    control,
    handleSubmit,
    reset,
    watch,
    formState: { errors, isSubmitting },
  } = useForm<ResolutionValues>({ resolver: zodResolver(resolutionSchema), defaultValues: { mode: 'legs', legs: [], reason: '' } })
  const legs = useFieldArray({ control, name: 'legs' })
  const mode = watch('mode')

  React.useEffect(() => {
    if (!item) return
    setServerError(null)
    chain.reset()
    reset({
      mode: item.kind.startsWith('WITHDRAWAL_UNKNOWN') ? 'withdrawal' : 'legs',
      outcome: undefined,
      external_id: '',
      fee: '',
      legs: [
        { account_code: '', owner_id: '', amount: '', memo: '' },
        { account_code: '', owner_id: '', amount: '', memo: '' },
      ],
      competence_date: '',
      description: '',
      reason: '',
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [item, reset])

  const onSubmit = async (v: ResolutionValues) => {
    if (!item) return
    setServerError(null)
    const body: Record<string, unknown> = { reason: v.reason, description: v.description || undefined }
    if (v.mode === 'withdrawal') {
      body.withdrawal_resolution = {
        withdrawal_id: item.internal_ref,
        outcome: v.outcome,
        external_id: v.external_id || undefined,
        fee_cents: v.fee ? parseBRLToCents(v.fee) ?? undefined : undefined,
      }
    } else {
      body.legs = v.legs.map((l) => ({ account_code: l.account_code, owner_id: l.owner_id || undefined, amount_cents: signedCents(l.amount), memo: l.memo || undefined }))
      if (v.competence_date) body.competence_date = v.competence_date
    }
    try {
      const r = await financeApi.reconResolve(item.id, body)
      await qc.invalidateQueries({ queryKey: ['finance'] })
      if (chain.handleCreated(r)) return
      toastApprovalCreated(r)
      onOpenChange(false)
    } catch (e) {
      setServerError(e)
    }
  }

  return (
    <Dialog open={!!item} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        {chain.step ?? (
          <>
        <DialogHeader>
          <DialogTitle>Resolver divergência por ajuste aprovado</DialogTitle>
          <DialogDescription>
            {item ? `${RECON_KIND_LABEL[item.kind] ?? item.kind}. ` : ''}O ajuste só é lançado depois que {copy.afterGate}.
          </DialogDescription>
        </DialogHeader>
        <ApprovalErrorAlert error={serverError} />
        <form id="recon-form" onSubmit={handleSubmit(onSubmit)} noValidate>
          <FieldGroup>
            {isWithdrawal ? (
              <FieldSet>
                <FieldLegend>Tipo de resolução</FieldLegend>
                <Controller
                  control={control}
                  name="mode"
                  render={({ field }) => (
                    <RadioGroup value={field.value} onValueChange={field.onChange} className="flex flex-col gap-2">
                      <Field orientation="horizontal" className="min-h-11 flex-nowrap">
                        <RadioGroupItem value="withdrawal" />
                        <FieldLabel className="min-w-0 flex-1 font-medium">Desfecho do saque conferido no painel da CloudPayPag</FieldLabel>
                      </Field>
                      <Field orientation="horizontal" className="min-h-11 flex-nowrap">
                        <RadioGroupItem value="legs" />
                        <FieldLabel className="min-w-0 flex-1 font-medium">Ajuste contábil (partidas)</FieldLabel>
                      </Field>
                    </RadioGroup>
                  )}
                />
              </FieldSet>
            ) : null}
            {mode === 'withdrawal' ? (
              <>
                <Field invalid={!!errors.outcome}>
                  <FieldLabel required>Desfecho</FieldLabel>
                  <Controller
                    control={control}
                    name="outcome"
                    render={({ field }) => (
                      <ToggleGroup type="single" value={field.value ?? ''} onValueChange={(v) => v && field.onChange(v)} aria-label="Desfecho do saque" className="self-start">
                        <ToggleGroupItem value="COMPLETED">Pago</ToggleGroupItem>
                        <ToggleGroupItem value="FAILED">Não pago</ToggleGroupItem>
                      </ToggleGroup>
                    )}
                  />
                  <FieldError>{errors.outcome?.message}</FieldError>
                </Field>
                <div className="grid grid-cols-1 gap-5 sm:grid-cols-2">
                  <Field>
                    <FieldLabel>ID do saque no provedor</FieldLabel>
                    <Input {...register('external_id')} />
                  </Field>
                  <Field>
                    <FieldLabel>Taxa cobrada (R$)</FieldLabel>
                    <Input inputMode="decimal" {...register('fee')} />
                  </Field>
                </div>
              </>
            ) : (
              <FieldSet>
                <FieldLegend>Partidas (débito positivo, crédito com “-”)</FieldLegend>
                {legs.fields.map((f, i) => (
                  <div key={f.id} className="grid grid-cols-1 gap-3 rounded-lg border border-border p-3 sm:grid-cols-[1fr_140px_auto]">
                    <Field invalid={!!errors.legs?.[i]?.account_code}>
                      <FieldLabel required>Conta {i + 1}</FieldLabel>
                      <Input className="font-mono" placeholder="RD:Cash:CloudPayPag" {...register(`legs.${i}.account_code`)} />
                      <FieldError>{errors.legs?.[i]?.account_code?.message}</FieldError>
                    </Field>
                    <Field invalid={!!errors.legs?.[i]?.amount}>
                      <FieldLabel required>Valor (R$)</FieldLabel>
                      <Input inputMode="decimal" placeholder="-10,00" {...register(`legs.${i}.amount`)} />
                      <FieldError>{errors.legs?.[i]?.amount?.message}</FieldError>
                    </Field>
                    <div className="flex items-end">
                      <Button type="button" variant="ghost" size="icon" aria-label={`Remover partida ${i + 1}`} disabled={legs.fields.length <= 2} onClick={() => legs.remove(i)}>
                        <Trash2 aria-hidden="true" />
                      </Button>
                    </div>
                    <Field className="sm:col-span-3">
                      <FieldLabel>Titular da conta (id, se houver)</FieldLabel>
                      <Input className="font-mono" {...register(`legs.${i}.owner_id`)} />
                    </Field>
                  </div>
                ))}
                {errors.legs?.message || errors.legs?.root?.message ? (
                  <p role="alert" className="m-0 text-xs font-medium text-danger">
                    {errors.legs?.message ?? errors.legs?.root?.message}
                  </p>
                ) : null}
                <Button type="button" variant="secondary" className="self-start" onClick={() => legs.append({ account_code: '', owner_id: '', amount: '', memo: '' })}>
                  <Plus aria-hidden="true" />
                  Adicionar partida
                </Button>
                <Field invalid={!!errors.competence_date}>
                  <FieldLabel>Competência do ajuste</FieldLabel>
                  <Input type="date" className="max-w-[200px]" {...register('competence_date')} />
                  <FieldDescription>Em branco = data de hoje. Períodos fechados não aceitam lançamentos.</FieldDescription>
                  <FieldError>{errors.competence_date?.message}</FieldError>
                </Field>
              </FieldSet>
            )}
            <Field>
              <FieldLabel>Descrição</FieldLabel>
              <Input {...register('description')} />
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
          <Button type="submit" form="recon-form" loading={isSubmitting} loadingText="Enviando…">
            {copy.submitLabel}
          </Button>
        </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}

export default function ReconciliationPage() {
  const qc = useQueryClient()
  const master = useIsAdminMaster()
  const runsTable = useTableState({ defaultPageSize: 10, prefix: 'run_' })
  const table = useTableState({ defaultPageSize: 25, filterKeys: ['status'] })
  const status = table.state.filters.status ?? 'DIVERGENT'
  const [resolving, setResolving] = React.useState<ReconItem | null>(null)

  const rParams = { page: runsTable.state.page, page_size: runsTable.state.pageSize }
  const runs = useQuery({ queryKey: qk.sub('finance', 'recon-runs', rParams), queryFn: () => financeApi.reconRuns(rParams), placeholderData: keepPreviousData })
  const iParams = { page: table.state.page, page_size: table.state.pageSize, status: status === 'all' ? undefined : status }
  const items = useQuery({ queryKey: qk.sub('finance', 'recon-items', iParams), queryFn: () => financeApi.reconItems(iParams), placeholderData: keepPreviousData })
  const runNow = useMutation({
    mutationFn: financeApi.reconRunNow,
    onSuccess: async (r) => {
      if (r.status === 'PROVIDER_UNAVAILABLE') toast.error('Provedor indisponível: a reconsulta não foi concluída.')
      else {
        const opened = Number((r.summary as { items_opened?: number } | null)?.items_opened ?? 0)
        toast.success(
          `Reconsulta concluída: ${opened === 0 ? 'nenhuma divergência nova' : opened === 1 ? '1 divergência nova' : `${formatNumber(opened)} divergências novas`}.`,
        )
      }
      // A reconsulta pode atualizar saques e abrir itens: invalida o domínio financeiro inteiro.
      await qc.invalidateQueries({ queryKey: ['finance'] })
    },
    onError: (e) => toast.error(errorMessage(e)),
  })

  const runCols: DataTableColumn<ReconRun>[] = [
    { id: 'started', header: 'Início', mobile: 'primary', cell: (r) => formatDateTime(r.started_at) },
    { id: 'kind', header: 'Tipo', mobile: 'field', cell: (r) => (r.kind === 'MANUAL' ? 'Manual' : r.kind === 'DAILY' ? 'Diária (automática)' : r.kind) },
    { id: 'provider', header: 'Saldo no provedor', numeric: true, mobile: 'field', cell: (r) => formatCents(r.provider_balance_cents) },
    { id: 'internal', header: 'Caixa interno', numeric: true, mobile: 'field', cell: (r) => formatCents(r.internal_cash_cents) },
    {
      id: 'summary',
      header: 'Resumo',
      mobile: 'field',
      cell: (r) => {
        const s = (r.summary ?? {}) as { withdrawals_checked?: number; withdrawals_updated?: number; items_opened?: number; error?: string }
        if (s.error) return <span className="text-danger">{s.error}</span>
        const count = (n: number | undefined, one: string, many: string) => `${formatNumber(n ?? null)} ${n === 1 ? one : many}`
        return [
          count(s.withdrawals_checked, 'saque conferido', 'saques conferidos'),
          count(s.withdrawals_updated, 'atualizado', 'atualizados'),
          count(s.items_opened, 'divergência', 'divergências'),
        ].join(' · ')
      },
    },
    { id: 'status', header: 'Situação', mobile: 'badge', cell: (r) => <StatusPill map={RECON_RUN_STATUS} value={r.status} /> },
  ]
  const itemCols: DataTableColumn<ReconItem>[] = [
    {
      id: 'kind',
      header: 'Divergência',
      mobile: 'primary',
      cell: (i) => (
        <div className="flex flex-col gap-0.5">
          <span className="font-semibold">{RECON_KIND_LABEL[i.kind] ?? i.kind}</span>
          {typeof i.detail?.note === 'string' ? <span className="text-xs text-text-muted">{i.detail.note}</span> : null}
        </div>
      ),
    },
    { id: 'internal_ref', header: 'Ref. interna', mobile: 'field', cell: (i) => <span className="font-mono text-xs">{i.internal_ref ?? EMPTY_VALUE}</span> },
    { id: 'external_ref', header: 'Ref. provedor', mobile: 'field', cell: (i) => <span className="font-mono text-xs">{i.external_ref ?? EMPTY_VALUE}</span> },
    { id: 'internal', header: 'Valor interno', numeric: true, mobile: 'field', cell: (i) => formatCents(i.internal_amount_cents) },
    { id: 'external', header: 'Valor no provedor', numeric: true, mobile: 'field', cell: (i) => formatCents(i.external_amount_cents) },
    { id: 'created', header: 'Aberta em', mobile: 'secondary', cell: (i) => formatDateTime(i.created_at) },
    {
      id: 'status',
      header: 'Situação',
      mobile: 'badge',
      cell: (i) => (
        <div className="flex flex-col gap-1">
          <StatusPill map={RECON_STATUS} value={i.status} />
        </div>
      ),
    },
  ]

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Conciliação"
        description="CloudPayPag × ledger"
        breadcrumbs={[{ label: 'Financeiro', to: '/financeiro' }, { label: 'Conciliação' }]}
        actions={
          <Can permission="finance.reconcile">
            <Button onClick={() => runNow.mutate()} loading={runNow.isPending} loadingText="Reconsultando…">
              <RefreshCw aria-hidden="true" />
              Reconsultar agora
            </Button>
          </Can>
        }
      />
      <LiveStale updatedAt={items.dataUpdatedAt} onRefresh={() => void items.refetch()} refreshing={items.isFetching} />
      <Alert variant="info">
        <GitCompareArrows aria-hidden="true" />
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <AlertTitle>O que a reconsulta cobre</AlertTitle>
          <AlertDescription>
            Saldo da conta e saques (consulta disponível na API). Cobranças não podem ser consultadas na CloudPayPag — BLOQUEADO POR DEPENDÊNCIA EXTERNA; elas são conciliadas pelos webhooks assinados. Divergências só são resolvidas por ajuste aprovado pelos dois Admin Master.
          </AlertDescription>
        </div>
      </Alert>
      <Card>
        <CardHeader>
          <div className="flex flex-col gap-1">
            <CardTitle>Itens de conciliação</CardTitle>
            <CardDescription>Saques com resultado desconhecido NÃO devem ser reenviados — confira no painel do provedor.</CardDescription>
          </div>
        </CardHeader>
        <DataTable
          caption="Itens de conciliação"
          columns={itemCols}
          data={items.data}
          getRowId={(i) => i.id}
          controller={table}
          isLoading={items.isLoading}
          isFetching={items.isFetching}
          error={items.error}
          onRetry={() => void items.refetch()}
          filtersActive={status !== 'DIVERGENT'}
          empty={{ title: 'Nenhuma divergência em aberto.', icon: GitCompareArrows }}
          toolbar={
            <div className="flex flex-col gap-2">
              <span id="recon-filter" className="text-sm font-semibold text-text">
                Situação
              </span>
              <ToggleGroup type="single" value={status} aria-labelledby="recon-filter" onValueChange={(v) => v && table.setFilter('status', v === 'DIVERGENT' ? null : v)}>
                <ToggleGroupItem value="DIVERGENT">Divergentes</ToggleGroupItem>
                <ToggleGroupItem value="RESOLVED">Resolvidos</ToggleGroupItem>
                <ToggleGroupItem value="all">Todos</ToggleGroupItem>
              </ToggleGroup>
            </div>
          }
          rowActions={
            master
              ? (i) =>
                  i.status === 'DIVERGENT' ? (
                    <Button variant="secondary" onClick={() => setResolving(i)} aria-label={`Resolver divergência ${RECON_KIND_LABEL[i.kind] ?? i.kind}`}>
                      Resolver por ajuste
                    </Button>
                  ) : null
              : undefined
          }
        />
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Execuções</CardTitle>
        </CardHeader>
        <DataTable
          caption="Execuções de conciliação"
          columns={runCols}
          data={runs.data}
          getRowId={(r) => r.id}
          controller={runsTable}
          isLoading={runs.isLoading}
          isFetching={runs.isFetching}
          error={runs.error}
          onRetry={() => void runs.refetch()}
          empty={{ title: 'Nenhuma conciliação executada ainda.', description: 'A conciliação diária roda automaticamente; use “Reconsultar agora” para antecipar.' }}
        />
      </Card>
      <ResolutionDialog item={resolving} onOpenChange={(o) => !o && setResolving(null)} />
    </div>
  )
}
