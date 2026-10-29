/**
 * Financeiro → Saques (RD): acompanhamento dos saques dos estabelecimentos (manuais e
 * automáticos do dia 1) e dos PIX OUT da RD; solicitação de RETIRADA DA RD (PIX OUT) por
 * pedido de aprovação (dupla: solicitação → pendente → aprovação do outro Admin Master; única:
 * o próprio Master autoriza no mesmo diálogo).
 * Tempo real: `withdrawal.updated` invalida 'withdrawals'.
 */
import * as React from 'react'
import { Controller, useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowUpFromLine, Banknote } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { StatusPill } from '@/components/finance/status-pill'
import { PIX_KEY_TYPES, WITHDRAWAL_KIND, WITHDRAWAL_ORIGIN, WITHDRAWAL_STATUS } from '@/components/finance/labels'
import { useEstablishmentNames } from '@/components/finance/hooks'
import { approvalCopy, useIsAdminMaster, toastApprovalCreated, useInstanceApprovalMode } from '@/components/finance/approval-utils'
import { ApprovalErrorAlert } from '@/components/finance/approval-errors'
import { useAuthorizationChain } from '@/components/finance/authorize-chain'
import { parseBRLToCents } from '@/components/finance/money'
import { LiveStale } from '@/components/finance/live-stale'
import { useTableState } from '@/hooks/useTableState'
import { qk } from '@/lib/queryKeys'
import { newRequestId } from '@/lib/http'
import { pixKeyFieldError } from '@/components/finance/payment-errors'
import { EMPTY_VALUE, formatCents, formatDateTime } from '@/lib/format'
import { financeApi, type Withdrawal } from './api'

const rdSchema = z.object({
  amount: z.string().trim().refine((v) => (parseBRLToCents(v) ?? 0) > 0, 'Valor inválido. Use o formato 1.234,56.'),
  pix_key_type: z.string().min(1, 'Escolha o tipo de chave.'),
  pix_key: z.string().trim().min(3, 'Informe a chave PIX de destino.').max(120),
  purpose: z.string().trim().max(200).optional(),
  reason: z.string().trim().min(10, 'Explique o motivo (mínimo 10 caracteres).').max(2000),
})
type RdValues = z.infer<typeof rdSchema>

function RdWithdrawalDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient()
  const [error, setError] = React.useState<unknown>(null)
  const idem = React.useRef<string>(newRequestId())
  const chain = useAuthorizationChain(() => onOpenChange(false))
  const instanceMode = useInstanceApprovalMode().mode
  const copy = approvalCopy(instanceMode)
  const { register, control, handleSubmit, reset, setError: setFieldError, formState: { errors, isSubmitting } } = useForm<RdValues>({
    resolver: zodResolver(rdSchema),
    defaultValues: { amount: '', pix_key_type: '', pix_key: '', purpose: '', reason: '' },
  })
  React.useEffect(() => {
    if (open) {
      setError(null)
      chain.reset()
      idem.current = newRequestId()
      reset({ amount: '', pix_key_type: '', pix_key: '', purpose: '', reason: '' })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, reset])
  const onSubmit = async (v: RdValues) => {
    setError(null)
    try {
      const r = await financeApi.createApproval(
        {
          action_type: 'RD_WITHDRAWAL',
          payload: { amount_cents: parseBRLToCents(v.amount), pix_key_type: v.pix_key_type, pix_key: v.pix_key, purpose: v.purpose || undefined },
          reason: v.reason,
        },
        idem.current,
      )
      await qc.invalidateQueries({ queryKey: ['finance'] })
      if (chain.handleCreated(r)) return
      toastApprovalCreated(r)
      onOpenChange(false)
    } catch (e) {
      const keyErr = pixKeyFieldError(e)
      if (keyErr) setFieldError('pix_key', { message: keyErr })
      else setError(e)
    }
  }
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        {chain.step ?? (
          <>
        <DialogHeader>
          <DialogTitle>Retirada da RD (PIX OUT)</DialogTitle>
          <DialogDescription>
            {instanceMode === 'SINGLE'
              ? 'Cria um pedido PENDENTE. O dinheiro só sai depois que você autorizar com código do autenticador, senha financeira e código por e-mail.'
              : 'Cria um pedido PENDENTE. O dinheiro só sai depois que o outro Admin Master aprovar com código do autenticador, senha financeira e código por e-mail.'}
          </DialogDescription>
        </DialogHeader>
        <ApprovalErrorAlert error={error} />
        <form id="rd-wd" onSubmit={handleSubmit(onSubmit)} noValidate>
          <FieldGroup>
            <Field invalid={!!errors.amount}>
              <FieldLabel required>Valor (R$)</FieldLabel>
              <Input inputMode="decimal" className="max-w-[220px]" {...register('amount')} />
              <FieldError>{errors.amount?.message}</FieldError>
            </Field>
            <div className="grid grid-cols-1 gap-5 sm:grid-cols-2">
              <Field invalid={!!errors.pix_key_type}>
                <FieldLabel required>Tipo de chave</FieldLabel>
                <Controller
                  control={control}
                  name="pix_key_type"
                  render={({ field }) => (
                    <Select value={field.value || undefined} onValueChange={field.onChange}>
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
                <FieldLabel required>Chave PIX de destino</FieldLabel>
                <Input autoComplete="off" {...register('pix_key')} />
                <FieldDescription>Fica cifrada; o aprovador vê a versão mascarada.</FieldDescription>
                <FieldError>{errors.pix_key?.message}</FieldError>
              </Field>
            </div>
            <Field>
              <FieldLabel>Finalidade</FieldLabel>
              <Input {...register('purpose')} placeholder="ex.: retirada dos sócios" />
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
          <Button type="submit" form="rd-wd" loading={isSubmitting} loadingText="Enviando…">
            {copy.submitLabel}
          </Button>
        </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}

const STATUS = ['REQUESTED', 'EXECUTING', 'PROCESSING', 'COMPLETED', 'FAILED', 'CANCELLED', 'UNKNOWN']

export default function WithdrawalsAdminPage() {
  const master = useIsAdminMaster()
  const ests = useEstablishmentNames()
  const table = useTableState({ defaultPageSize: 25, filterKeys: ['status', 'establishment_id'] })
  const [open, setOpen] = React.useState(false)
  const params = { ...table.apiParams, sort: undefined, q: undefined }
  const q = useQuery({ queryKey: qk.list('withdrawals', params), queryFn: () => financeApi.withdrawals(params), placeholderData: keepPreviousData })

  const columns: DataTableColumn<Withdrawal>[] = [
    { id: 'requested', header: 'Solicitado em', mobile: 'secondary', cell: (w) => formatDateTime(w.requested_at) },
    {
      id: 'who',
      header: 'Origem',
      mobile: 'primary',
      cell: (w) => (
        <div className="flex flex-col">
          <span className="font-semibold">{w.establishment_id ? ests.nameOf(w.establishment_id) : (WITHDRAWAL_KIND[w.kind] ?? w.kind)}</span>
          <span className="text-xs text-text-muted">
            {WITHDRAWAL_KIND[w.kind] ?? w.kind} · {WITHDRAWAL_ORIGIN[w.origin] ?? w.origin}
          </span>
        </div>
      ),
    },
    { id: 'amount', header: 'Valor', numeric: true, mobile: 'field', cell: (w) => formatCents(w.amount_cents) },
    { id: 'fee', header: 'Taxa', numeric: true, mobile: 'field', cell: (w) => formatCents(w.fee_cents) },
    { id: 'dest', header: 'Destino', mobile: 'field', cell: (w) => <span className="font-mono text-xs">{w.destination_masked ?? EMPTY_VALUE}</span> },
    { id: 'ext', header: 'ID no provedor', mobile: 'hidden', cell: (w) => <span className="font-mono text-xs">{w.provider_external_id ?? EMPTY_VALUE}</span> },
    {
      id: 'status',
      header: 'Situação',
      mobile: 'badge',
      cell: (w) => (
        <div className="flex flex-col gap-1">
          <StatusPill map={WITHDRAWAL_STATUS} value={w.status} />
          {w.failure_reason ? <span className="text-xs text-danger">{w.failure_reason}</span> : null}
        </div>
      ),
    },
  ]

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Saques"
        description="Saques de estabelecimentos e PIX OUT da RD"
        breadcrumbs={[{ label: 'Financeiro', to: '/financeiro' }, { label: 'Saques' }]}
        actions={
          master ? (
            <Button onClick={() => setOpen(true)}>
              <ArrowUpFromLine aria-hidden="true" />
              Retirada da RD (PIX OUT)
            </Button>
          ) : undefined
        }
      />
      <LiveStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />
      <p className="m-0 text-sm text-text-muted">
        Saques de estabelecimentos não passam por dupla aprovação (confirmados pelo próprio estabelecimento com autenticador e senha financeira). Saques com situação “Em conferência manual” NÃO devem ser reenviados — resolva em Conciliação.
      </p>
      <Card>
        <DataTable
          caption="Saques"
          columns={columns}
          data={q.data}
          getRowId={(w) => w.id}
          controller={table}
          isLoading={q.isLoading}
          isFetching={q.isFetching}
          error={q.error}
          onRetry={() => void q.refetch()}
          filtersActive={Object.keys(table.state.filters).length > 0}
          empty={{ title: 'Nenhum saque registrado.', icon: Banknote }}
          toolbar={
            <>
              <Field className="w-full max-w-[240px]">
                <FieldLabel>Situação</FieldLabel>
                <Select value={table.state.filters.status ?? 'all'} onValueChange={(v) => table.setFilter('status', v === 'all' ? null : v)}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      <SelectItem value="all">Todas</SelectItem>
                      {STATUS.map((s) => (
                        <SelectItem key={s} value={s}>
                          {WITHDRAWAL_STATUS[s]?.label ?? s}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  </SelectContent>
                </Select>
              </Field>
              <Field className="w-full max-w-sm">
                <FieldLabel>Estabelecimento</FieldLabel>
                <Select value={table.state.filters.establishment_id ?? 'all'} onValueChange={(v) => table.setFilter('establishment_id', v === 'all' ? null : v)}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      <SelectItem value="all">Todos</SelectItem>
                      {ests.items.map((e) => (
                        <SelectItem key={e.id} value={e.id}>
                          {e.trade_name || e.name}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  </SelectContent>
                </Select>
              </Field>
            </>
          }
        />
      </Card>
      <RdWithdrawalDialog open={open} onOpenChange={setOpen} />
    </div>
  )
}
