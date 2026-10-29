/**
 * Minha unidade → Financeiro → Saques (P001 §86; P002 item 11; ETAPA2 #643–#646).
 * Saldo disponível/pendente/pago/negativo (bloqueio), simulação da taxa, solicitação de
 * saque MANUAL com TOTP + senha financeira (sem dupla aprovação; `Idempotency-Key`),
 * histórico e chave PIX ativa (SOMENTE leitura — troca só pelo suporte RD, com bloqueio de
 * 12 h). O saque NUNCA aparece na visão geral (dashboard).
 * Tempo real: `withdrawal.updated` invalida 'withdrawals'.
 */
import * as React from 'react'
import { Link } from 'react-router-dom'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Banknote, Calculator, KeyRound, Lock, Store } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Skeleton } from '@/components/ui/skeleton'
import { KpiCard } from '@/components/patterns/kpi-card'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { EmptyState, ErrorState, PageSkeleton } from '@/components/patterns/states'
import { Can } from '@/components/auth/guards'
import { StatusPill } from '@/components/finance/status-pill'
import { DetailList } from '@/components/finance/detail-list'
import { PIX_KEY_STATUS, WITHDRAWAL_ORIGIN, WITHDRAWAL_STATUS, pixKeyTypeLabel } from '@/components/finance/labels'
import { FinanceSecurityNotice, StepUpFields } from '@/components/finance/security'
import { useFinanceReady } from '@/components/finance/security-status'
import { useContextOrgId, useSelectedEstablishment } from '@/components/finance/hooks'
import { EstablishmentPicker } from '@/components/finance/establishment-picker'
import { LiveStale } from '@/components/finance/live-stale'
import { emailCodeSchema, financialPasswordSchema, stepUpFieldFromError, totpSchema } from '@/components/finance/step-up'
import { EmailCodeField } from '@/components/finance/email-code-field'
import { emailCodeErrorInfo } from '@/components/finance/email-code'
import { parseBRLToCents } from '@/components/finance/money'
import { useTableState } from '@/hooks/useTableState'
import { useCan } from '@/hooks/usePermissions'
import { qk } from '@/lib/queryKeys'
import { newRequestId } from '@/lib/http'
import { errorMessage, isApiError } from '@/lib/errors'
import { DEFAULT_TIMEZONE, EMPTY_VALUE, formatCents, formatDateTime } from '@/lib/format'
import { financeApi, type PixKey, type Withdrawal, type WithdrawalSimulation } from './api'

const schema = z.object({
  amount: z
    .string()
    .trim()
    .min(1, 'Informe o valor do saque.')
    .refine((v) => (parseBRLToCents(v) ?? 0) > 0, 'Valor inválido. Use o formato 1.234,56.'),
  totp_code: totpSchema,
  financial_password: financialPasswordSchema,
  // Obrigatório em TODO saque (W-FIN pós-janela d); vale só para este estabelecimento e este valor.
  email_code: emailCodeSchema,
})
type Values = z.infer<typeof schema>

function currentKey(keys: PixKey[] | undefined): PixKey | null {
  return (keys ?? []).find((k) => k.status !== 'REVOKED') ?? null
}

function PixKeyCard({ establishmentId, timeZone }: { establishmentId: string; timeZone: string }) {
  const q = useQuery({
    queryKey: qk.sub('withdrawals', 'pix-keys', establishmentId),
    queryFn: () => financeApi.pixKeys(establishmentId),
  })
  const key = currentKey(q.data?.items)
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Chave PIX de recebimento</CardTitle>
          <CardDescription>Somente leitura. Para cadastrar ou trocar a chave, fale com o suporte da RD.</CardDescription>
        </div>
        <KeyRound aria-hidden="true" className="size-5 text-text-muted" />
      </CardHeader>
      {q.isLoading ? (
        <Skeleton className="h-20 w-full" />
      ) : q.error ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} size="compact" />
      ) : !key ? (
        <EmptyState
          size="compact"
          headingLevel="h3"
          title="Nenhuma chave PIX cadastrada"
          description="Sem chave ativa não é possível sacar. Solicite o cadastro pelo suporte; a chave nova fica bloqueada por 12 horas por segurança."
          action={
            <Button asChild variant="secondary">
              <Link to="/unidade/suporte">Abrir chamado no suporte</Link>
            </Button>
          }
        />
      ) : (
        <div className="flex flex-col gap-3">
          <div>
            <StatusPill map={PIX_KEY_STATUS} value={key.status} />
          </div>
          <DetailList
            items={[
              { label: 'Tipo', value: pixKeyTypeLabel(key.key_type) },
              { label: 'Chave', value: <span className="font-mono">{key.key_masked}</span> },
              { label: 'Titular', value: key.holder_name },
              { label: 'Documento do titular', value: <span className="font-mono">{key.holder_document_masked}</span> },
              { label: 'Cadastrada em', value: formatDateTime(key.created_at, timeZone) },
              {
                label: key.status === 'COOLDOWN' ? 'Liberada para saques em' : 'Ativa desde',
                value: formatDateTime(key.active_from, timeZone),
              },
            ]}
          />
          {key.status === 'COOLDOWN' ? (
            <Alert variant="warning">
              <Lock aria-hidden="true" />
              <AlertDescription>
                Chave alterada recentemente: por segurança, os saques ficam bloqueados até {formatDateTime(key.active_from, timeZone)}.
              </AlertDescription>
            </Alert>
          ) : null}
        </div>
      )}
    </Card>
  )
}

function WithdrawalForm({ establishmentId, availableCents, blocked }: { establishmentId: string; availableCents: number | null; blocked: boolean }) {
  const qc = useQueryClient()
  const { query: sec, ready } = useFinanceReady()
  const [serverError, setServerError] = React.useState<string | null>(null)
  const [sim, setSim] = React.useState<WithdrawalSimulation | null>(null)
  // A mesma chave de idempotência protege reenvios do MESMO pedido (ex.: rede caiu).
  const idem = React.useRef<{ amount: number; key: string } | null>(null)
  // Valor para o qual o código por e-mail foi enviado; se o valor mudar, o código deixa de valer.
  const [codeFor, setCodeFor] = React.useState<number | null>(null)
  const [codeReset, setCodeReset] = React.useState(0)
  const {
    register,
    handleSubmit,
    getValues,
    setError,
    clearErrors,
    watch,
    reset,
    resetField,
    trigger,
    formState: { errors, isSubmitting },
  } = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { amount: '', totp_code: '', financial_password: '', email_code: '' } })
  const amountCents = parseBRLToCents(watch('amount') ?? '')

  const invalidateCode = () => {
    setCodeFor(null)
    resetField('email_code')
    setCodeReset((n) => n + 1)
  }

  const sendCode = async () => {
    const cents = parseBRLToCents(getValues('amount'))
    if (!cents) throw new Error('Informe um valor válido antes de pedir o código.')
    const r = await financeApi.withdrawalEmailCode(establishmentId, cents)
    setCodeFor(cents)
    clearErrors('email_code')
    toast.success(`Código enviado para o seu e-mail (saque de ${formatCents(cents)}).`)
    return r
  }

  const simulate = useMutation({
    mutationFn: (cents: number) => financeApi.simulateWithdrawal(establishmentId, cents),
    onSuccess: (r) => {
      setSim(r)
      setServerError(null)
    },
    onError: (e) => {
      setSim(null)
      setServerError(errorMessage(e))
    },
  })

  const onSimulate = async () => {
    const ok = await trigger('amount')
    if (!ok) return
    const cents = parseBRLToCents(getValues('amount'))
    if (cents) simulate.mutate(cents)
  }

  const onSubmit = async (v: Values) => {
    setServerError(null)
    const cents = parseBRLToCents(v.amount) as number
    if (codeFor !== null && codeFor !== cents) {
      invalidateCode()
      setError('email_code', { message: 'O valor mudou depois do envio do código. Peça um novo código para este valor.' })
      return
    }
    if (!idem.current || idem.current.amount !== cents) idem.current = { amount: cents, key: newRequestId() }
    try {
      const w = await financeApi.requestWithdrawal(
        {
          establishment_id: establishmentId,
          amount_cents: cents,
          totp_code: v.totp_code.trim(),
          financial_password: v.financial_password,
          email_code: v.email_code.trim(),
        },
        idem.current.key,
      )
      idem.current = null
      setCodeFor(null)
      reset()
      setCodeReset((n) => n + 1)
      setSim(null)
      toast.success(`Saque de ${formatCents(w.amount_cents)} solicitado. Acompanhe o status no histórico.`)
      await qc.invalidateQueries({ queryKey: ['withdrawals'] })
    } catch (e) {
      // TOTP é de uso único; valor, senha financeira e demais campos são preservados.
      resetField('totp_code')
      const codeInfo = emailCodeErrorInfo(e)
      if (codeInfo) {
        if (codeInfo.resetCode) invalidateCode()
        if (codeInfo.target === 'email_code') setError('email_code', { message: codeInfo.message })
        else setServerError(codeInfo.message)
        if (isApiError(e) && e.status !== 0 && e.status < 500) idem.current = null
      } else if (isApiError(e)) {
        const field = stepUpFieldFromError(e.code)
        if (e.code === 'INSUFFICIENT_BALANCE' || e.code === 'WITHDRAWAL_AMOUNT_INVALID') setError('amount', { message: e.message })
        else if (field === 'totp_code' || field === 'financial_password') setError(field, { message: e.message })
        else setServerError(e.message)
        // Erro definitivo de negócio: próxima tentativa é um novo pedido.
        if (e.status !== 0 && e.status < 500) idem.current = null
      } else setServerError(errorMessage(e))
    }
  }

  const disabled = blocked || !ready
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Solicitar saque</CardTitle>
          <CardDescription>
            Saque do saldo disponível para a chave PIX ativa. Confirmação com o código do autenticador, a senha financeira e o código enviado por e-mail.
          </CardDescription>
        </div>
        <Banknote aria-hidden="true" className="size-5 text-text-muted" />
      </CardHeader>
      <FinanceSecurityNotice status={sec.data} loading={sec.isLoading} error={sec.error} onRetry={() => void sec.refetch()} />
      {blocked ? (
        <Alert variant="danger">
          <Lock aria-hidden="true" />
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <AlertTitle>Saques bloqueados</AlertTitle>
            <AlertDescription>Há saldo negativo por estorno de comissão já sacada. O bloqueio sai quando o saldo for compensado.</AlertDescription>
          </div>
        </Alert>
      ) : null}
      {serverError ? (
        <Alert variant="danger">
          <AlertDescription>{serverError}</AlertDescription>
        </Alert>
      ) : null}
      <form onSubmit={handleSubmit(onSubmit)} noValidate className="flex flex-col gap-5" aria-label="Solicitar saque">
        <FieldGroup>
          <Field invalid={!!errors.amount} disabled={disabled}>
            <FieldLabel required>Valor do saque (R$)</FieldLabel>
            <div className="flex flex-wrap gap-2">
              <Input inputMode="decimal" placeholder="0,00" className="max-w-[220px]" {...register('amount', {
                  onChange: () => {
                    setSim(null)
                  },
                })} />
              <Button type="button" variant="secondary" onClick={onSimulate} loading={simulate.isPending} loadingText="Simulando…" disabled={disabled}>
                <Calculator aria-hidden="true" />
                Simular taxa
              </Button>
            </div>
            <FieldDescription>Disponível agora: {formatCents(availableCents)}. A taxa do PIX é descontada do valor.</FieldDescription>
            <FieldError>{errors.amount?.message}</FieldError>
          </Field>
          {sim ? (
            <div role="status" className="rounded-lg border border-border p-4">
              <DetailList
                columns={3}
                items={[
                  { label: 'Valor solicitado', value: formatCents(sim.amount_cents), numeric: true },
                  { label: 'Taxa do provedor', value: formatCents(sim.fee_cents), numeric: true },
                  { label: 'Você recebe', value: formatCents(sim.net_cents), numeric: true },
                  { label: 'Destino', value: <span className="font-mono">{sim.destination_masked}</span> },
                  { label: 'Saldo suficiente', value: sim.sufficient ? 'Sim' : 'Não' },
                  { label: 'Provedor', value: sim.provider_operational ? 'Operacional' : 'Indisponível no momento' },
                ]}
              />
            </div>
          ) : null}
          <StepUpFields register={register} errors={errors} disabled={disabled} />
          <EmailCodeField
            registration={register('email_code')}
            error={errors.email_code?.message}
            disabled={disabled}
            canSend={!!amountCents && amountCents > 0}
            cannotSendReason="Informe o valor do saque para pedir o código."
            onSend={sendCode}
            resetSignal={codeReset}
            hint={codeFor !== null && amountCents !== codeFor ? 'O valor mudou: peça um novo código.' : 'Vale só para este valor.'}
          />
        </FieldGroup>
        <Button type="submit" loading={isSubmitting} loadingText="Solicitando…" disabled={disabled} className="self-start">
          <Banknote aria-hidden="true" />
          Solicitar saque
        </Button>
      </form>
    </Card>
  )
}

export default function EstablishmentWithdrawalsPage() {
  const orgId = useContextOrgId()
  const can = useCan()
  const { query: estQ, items, selected, select } = useSelectedEstablishment(orgId)
  const estId = selected?.id ?? null
  const tz = selected?.timezone || DEFAULT_TIMEZONE
  const table = useTableState({ defaultPageSize: 10, filterKeys: [] })

  const balance = useQuery({
    queryKey: qk.sub('withdrawals', 'balance', estId ?? ''),
    queryFn: () => financeApi.establishmentBalance(estId as string),
    enabled: !!estId,
  })
  const params = { establishment_id: estId ?? undefined, page: table.state.page, page_size: table.state.pageSize }
  const history = useQuery({
    queryKey: qk.list('withdrawals', params),
    queryFn: () => financeApi.withdrawals(params),
    enabled: !!estId,
    placeholderData: keepPreviousData,
  })

  if (!orgId) {
    return (
      <>
        <PageHeader title="Saques" />
        <EmptyState icon={Store} title="Selecione a sua unidade" description="Use o seletor de contexto para entrar em “Minha unidade”." />
      </>
    )
  }
  if (estQ.isLoading) return <PageSkeleton label="Carregando saques…" />
  if (estQ.error) return <ErrorState error={estQ.error} onRetry={() => void estQ.refetch()} />
  if (!estId) {
    return (
      <>
        <PageHeader title="Saques" />
        <EmptyState icon={Store} title="Nenhum estabelecimento vinculado" description="Sua organização ainda não tem estabelecimento ativo." />
      </>
    )
  }

  const b = balance.data
  const pendingTotal = b ? b.pending_closed_cents + b.pending_provisional_cents : null
  const columns: DataTableColumn<Withdrawal>[] = [
    { id: 'requested_at', header: 'Solicitado em', mobile: 'primary', cell: (w) => formatDateTime(w.requested_at, tz) },
    { id: 'amount', header: 'Valor', numeric: true, mobile: 'field', cell: (w) => formatCents(w.amount_cents) },
    { id: 'fee', header: 'Taxa', numeric: true, mobile: 'field', cell: (w) => formatCents(w.fee_cents) },
    { id: 'net', header: 'Líquido', numeric: true, mobile: 'field', cell: (w) => formatCents(w.net_cents) },
    { id: 'dest', header: 'Destino', mobile: 'field', cell: (w) => <span className="font-mono text-xs">{w.destination_masked ?? EMPTY_VALUE}</span> },
    { id: 'origin', header: 'Origem', mobile: 'field', cell: (w) => WITHDRAWAL_ORIGIN[w.origin] ?? w.origin },
    {
      id: 'status',
      header: 'Situação',
      mobile: 'badge',
      cell: (w) => (
        <div className="flex flex-col gap-1">
          <StatusPill map={WITHDRAWAL_STATUS} value={w.status} />
          {w.failure_reason ? <span className="text-xs text-danger">{w.failure_reason}</span> : null}
          {w.completed_at ? <span className="text-xs text-text-muted">Pago em {formatDateTime(w.completed_at, tz)}</span> : null}
        </div>
      ),
    },
  ]

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Saques"
        description="Financeiro → Saques: saldo, solicitação de saque e histórico."
        breadcrumbs={[{ label: 'Minha unidade', to: '/unidade' }, { label: 'Financeiro' }, { label: 'Saques' }]}
      />
      <EstablishmentPicker items={items} selectedId={estId} onSelect={select} />
      <LiveStale updatedAt={balance.dataUpdatedAt} onRefresh={() => void balance.refetch()} refreshing={balance.isFetching} />
      {balance.error ? (
        <ErrorState error={balance.error} onRetry={() => void balance.refetch()} size="compact" />
      ) : (
        <section aria-label="Saldo" className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4 xl:gap-6">
          <KpiCard label="Disponível para saque" value={b?.available_cents} format={formatCents} loading={balance.isLoading} description="Liberado no dia 1 de cada mês." />
          <KpiCard
            label="Pendente"
            value={pendingTotal}
            format={formatCents}
            loading={balance.isLoading}
            emphasis="secondary"
            description={b ? `Fechado (libera no dia 1): ${formatCents(b.pending_closed_cents)} · mês em curso (provisório): ${formatCents(b.pending_provisional_cents)}` : undefined}
          />
          <KpiCard label="Em processamento" value={b?.in_transit_cents} format={formatCents} loading={balance.isLoading} emphasis="secondary" description="Saques solicitados aguardando o banco." />
          <KpiCard label="Já pago" value={b?.paid_cents} format={formatCents} loading={balance.isLoading} emphasis="secondary" description="Total de saques concluídos." />
        </section>
      )}
      {b?.withdrawals_blocked ? (
        <Alert variant="danger">
          <Lock aria-hidden="true" />
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <AlertTitle>Saldo negativo: {formatCents(b.negative_balance_cents)}</AlertTitle>
            <AlertDescription>Estorno de comissão já sacada. Novos saques ficam bloqueados até a compensação.</AlertDescription>
          </div>
        </Alert>
      ) : null}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-12 xl:gap-6">
        <div className="xl:col-span-7">
          <Can
            permission="withdrawals.request"
            otherwise={
              <Card>
                <EmptyState
                  size="compact"
                  headingLevel="h3"
                  icon={Lock}
                  title="Seu perfil só visualiza os saques"
                  description="Peça ao responsável pela unidade a permissão para solicitar saques."
                />
              </Card>
            }
          >
            <WithdrawalForm establishmentId={estId} availableCents={b?.available_cents ?? null} blocked={!!b?.withdrawals_blocked} />
          </Can>
        </div>
        <div className="xl:col-span-5">
          <PixKeyCard establishmentId={estId} timeZone={tz} />
        </div>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>Histórico de saques</CardTitle>
        </CardHeader>
        <DataTable
          caption="Histórico de saques"
          columns={columns}
          data={history.data}
          getRowId={(w) => w.id}
          controller={table}
          isLoading={history.isLoading}
          isFetching={history.isFetching}
          error={history.error}
          onRetry={() => void history.refetch()}
          empty={{
            title: 'Nenhum saque realizado ainda.',
            description: can('withdrawals.request') ? 'Quando houver saldo disponível, solicite o saque acima.' : undefined,
            icon: Banknote,
          }}
        />
      </Card>
    </div>
  )
}
