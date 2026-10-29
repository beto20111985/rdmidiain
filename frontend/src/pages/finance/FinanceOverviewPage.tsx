/**
 * Financeiro → Visão financeira (ETAPA2 #651; PG-19): recebido, a receber, estornado,
 * taxas, comissões (despesa e saldo a pagar), saques pagos, caixa na CloudPayPag e contas a
 * pagar — tudo do ledger em partidas dobradas (`/financial/overview`). Pendências de dupla
 * aprovação e situação do provedor.
 */
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ShieldCheck } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { KpiCard } from '@/components/patterns/kpi-card'
import { ErrorState } from '@/components/patterns/states'
import { PeriodSelector } from '@/components/finance/period-selector'
import { periodLabel, usePeriodState } from '@/components/finance/period'
import { LiveStale } from '@/components/finance/live-stale'
import { useInstanceApprovalMode, useIsAdminMaster } from '@/components/finance/approval-utils'
import { useCan } from '@/hooks/usePermissions'
import { useAuthStore } from '@/stores/authStore'
import { qk } from '@/lib/queryKeys'
import { formatCents, formatNumber } from '@/lib/format'
import type { Permission } from '@/lib/permissions'
import { financeApi } from './api'
import { ProviderStatusCard } from './provider-status'

const LINKS: Array<{ to: string; label: string; perm: Permission }> = [
  { to: '/financeiro/recebiveis', label: 'Contas a receber', perm: 'finance.view' },
  { to: '/financeiro/pagaveis', label: 'Contas a pagar', perm: 'finance.view' },
  { to: '/financeiro/despesas', label: 'Despesas', perm: 'finance.view' },
  { to: '/financeiro/fluxo-de-caixa', label: 'Fluxo de caixa', perm: 'finance.view' },
  { to: '/financeiro/dre', label: 'DRE', perm: 'finance.view' },
  { to: '/financeiro/fechamento', label: 'Fechamento mensal', perm: 'finance.manage' },
  { to: '/financeiro/extratos', label: 'Extratos', perm: 'finance.view' },
  { to: '/financeiro/conciliacao', label: 'Conciliação', perm: 'finance.reconcile' },
  { to: '/financeiro/comissoes', label: 'Comissões', perm: 'commissions.view' },
  { to: '/financeiro/saques', label: 'Saques', perm: 'withdrawals.view' },
]

export default function FinanceOverviewPage() {
  const can = useCan()
  const master = useIsAdminMaster()
  const single = useInstanceApprovalMode().isSingle
  const meId = useAuthStore((s) => s.me?.user.id)
  const { period, setPeriod } = usePeriodState({ defaultPreset: '30' })
  const q = useQuery({
    queryKey: qk.sub('finance', 'overview', period.start, period.end),
    queryFn: () => financeApi.overview(period.start, period.end),
  })
  const pending = useQuery({
    queryKey: qk.sub('finance', 'approvals', 'pending-count'),
    queryFn: () => financeApi.approvals({ status: 'PENDING', page_size: 100 }),
  })
  const o = q.data
  const pendingTotal = pending.data?.total ?? null
  const awaitingMe = (pending.data?.items ?? []).filter((r) =>
    single ? r.approval_mode === 'SINGLE' && r.requested_by === meId && r.can_authorize !== false : r.can_approve && r.requested_by !== meId,
  ).length

  return (
    <div className="flex flex-col gap-6">
      <PageHeader title="Financeiro" description="Visão consolidada do ledger (dados conciliados)" />
      <PeriodSelector value={period} onChange={setPeriod} />
      <LiveStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />
      {pendingTotal ? (
        <Alert variant={master && awaitingMe ? 'warning' : 'info'}>
          <ShieldCheck aria-hidden="true" />
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <AlertTitle>
              {formatNumber(pendingTotal)} {pendingTotal === 1 ? 'pedido pendente' : 'pedidos pendentes'} de {single ? 'autorização' : 'dupla aprovação'}
            </AlertTitle>
            <AlertDescription>
              {master ? (
                <Link to="/financeiro/aprovacoes" className="font-semibold text-link underline-offset-4 hover:underline">
                  {awaitingMe ? `${awaitingMe} aguardando ${single ? 'a sua autorização' : 'a sua decisão'} — revisar` : 'Ver pedidos'}
                </Link>
              ) : (
                single ? 'Somente os Administradores Master autorizam operações financeiras.' : 'Somente os Admin Master aprovam operações financeiras.'
              )}
            </AlertDescription>
          </div>
        </Alert>
      ) : null}

      {q.error ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />
      ) : (
        <>
          <section aria-labelledby="recv-title" className="flex flex-col gap-3">
            <h2 id="recv-title" className="text-title-section text-text">
              Recebimentos · {periodLabel(period)}
            </h2>
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4 xl:gap-6">
              <KpiCard label="Recebido" value={o?.received_cents} format={formatCents} loading={q.isLoading} description="Pagamentos confirmados no período." />
              <KpiCard label="A receber" value={o?.pending_receivables_cents} format={formatCents} loading={q.isLoading} description="Cobranças pendentes (todas as datas)." />
              <KpiCard label="Reembolsado" value={o?.refunded_cents} format={formatCents} loading={q.isLoading} emphasis="secondary" description="Total acumulado de reembolsos." />
              <KpiCard label="Taxas CloudPayPag" value={o?.gateway_fees_cents} format={formatCents} loading={q.isLoading} emphasis="secondary" />
            </div>
          </section>
          <section aria-labelledby="pay-title" className="flex flex-col gap-3">
            <h2 id="pay-title" className="text-title-section text-text">
              Comissões, saques e saldos
            </h2>
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-5 xl:gap-6">
              <KpiCard label="Comissões (despesa)" value={o?.commission_expense_cents} format={formatCents} loading={q.isLoading} emphasis="secondary" description="Líquidas do resíduo de arredondamento da RD." />
              <KpiCard label="Comissão a pagar" value={o?.commission_payable_cents} format={formatCents} loading={q.isLoading} emphasis="secondary" description="Saldo devido a estabelecimentos." />
              <KpiCard label="Saques pagos" value={o?.withdrawals_paid_cents} format={formatCents} loading={q.isLoading} emphasis="secondary" description="No período." />
              <KpiCard label="Caixa na CloudPayPag" value={o?.cash_cloudpaypag_cents} format={formatCents} loading={q.isLoading} emphasis="secondary" description="Saldo contábil atual." />
              <KpiCard label="Contas a pagar" value={o?.accounts_payable_cents} format={formatCents} loading={q.isLoading} emphasis="secondary" description="Saldo em aberto." />
            </div>
          </section>
        </>
      )}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-12 xl:gap-6">
        <div className="xl:col-span-8">
          <ProviderStatusCard compact />
        </div>
        <Card className="xl:col-span-4">
          <CardHeader>
            <div className="flex flex-col gap-1">
              <CardTitle>Atalhos</CardTitle>
              <CardDescription>Áreas do financeiro disponíveis para o seu perfil.</CardDescription>
            </div>
          </CardHeader>
          <ul className="m-0 flex list-none flex-col gap-2 p-0">
            {LINKS.filter((l) => can(l.perm)).map((l) => (
              <li key={l.to}>
                <Link to={l.to} className="text-sm font-semibold text-link underline-offset-4 hover:underline">
                  {l.label}
                </Link>
              </li>
            ))}
            <li>
              <Link to="/financeiro/provedor" className="text-sm font-semibold text-link underline-offset-4 hover:underline">
                Status do provedor
              </Link>
            </li>
          </ul>
        </Card>
      </div>
    </div>
  )
}
