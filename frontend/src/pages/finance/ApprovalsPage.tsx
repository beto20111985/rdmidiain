/**
 * Financeiro → Aprovações (P001 §83–§85; P002 item 10; ADR_009; ADR_011).
 * Somente os Admin Master. Dois modos por instância:
 *  - DUPLA: lista de pendências com alerta, detalhe do conteúdo, aprovar (TOTP + senha financeira +
 *    código por e-mail), rejeitar, cancelar pelo solicitante. O solicitante NÃO aprova o próprio pedido.
 *  - ÚNICA: o próprio solicitante AUTORIZA ("Autorizar", rótulo "Aprovação única"); nada de outro Master.
 * `?pedido=<id>` abre o detalhe de um pedido (atalhos de outras telas).
 * Tempo real: `finance.approval_request` invalida o domínio 'finance'.
 */
import * as React from 'react'
import { useSearchParams } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { BellRing, ShieldCheck } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { NoPermission } from '@/components/patterns/states'
import { StatusPill } from '@/components/finance/status-pill'
import { APPROVAL_STATUS, EXECUTION_STATE } from '@/components/finance/labels'
import { FinanceSecurityNotice, FinancialPasswordChangedAt } from '@/components/finance/security'
import { useFinanceReady } from '@/components/finance/security-status'
import { personLabel } from '@/components/finance/names'
import { approvalModeNoun, useInstanceApprovalMode } from '@/components/finance/approval-utils'
import { LiveStale } from '@/components/finance/live-stale'
import { useTableState } from '@/hooks/useTableState'
import { useAuthStore } from '@/stores/authStore'
import { qk } from '@/lib/queryKeys'
import { formatCents, formatDateTime, EMPTY_VALUE } from '@/lib/format'
import { financeApi, type ApprovalRequest } from './api'
import { ApprovalDetailSheet } from './approval-detail'

const FILTERS = [
  { value: 'PENDING', label: 'Pendentes' },
  { value: 'APPROVED', label: 'Aprovados' },
  { value: 'REJECTED', label: 'Rejeitados' },
  { value: 'CANCELLED', label: 'Cancelados' },
  { value: 'EXPIRED', label: 'Expirados' },
  { value: 'all', label: 'Todos' },
]

export default function ApprovalsPage() {
  const me = useAuthStore((s) => s.me)
  const isMaster = !!me?.user.is_admin_master
  const table = useTableState({ defaultPageSize: 25, filterKeys: ['status'] })
  const status = table.state.filters.status ?? 'PENDING'
  const [searchParams] = useSearchParams()
  const [openId, setOpenId] = React.useState<string | null>(searchParams.get('pedido'))
  const { query: sec, ready } = useFinanceReady(isMaster)
  // Sem o modo (fixture/servidor anterior) = dupla, o comportamento original.
  const instanceMode = useInstanceApprovalMode().mode ?? 'DUAL'
  const single = instanceMode === 'SINGLE'

  const params = { page: table.state.page, page_size: table.state.pageSize, status: status === 'all' ? undefined : status }
  const list = useQuery({
    queryKey: qk.sub('finance', 'approvals', 'list', params),
    queryFn: () => financeApi.approvals(params),
    placeholderData: keepPreviousData,
    enabled: isMaster,
  })
  // Pendências que aguardam ESTA conta (alerta no topo), independente do filtro.
  const pending = useQuery({
    queryKey: qk.sub('finance', 'approvals', 'pending-count'),
    queryFn: () => financeApi.approvals({ status: 'PENDING', page_size: 100 }),
    enabled: isMaster,
  })
  const pendingItems = pending.data?.items ?? []
  const awaitingMe = single
    ? pendingItems.filter((r) => (r.approval_mode ?? instanceMode) === 'SINGLE' && r.requested_by === me?.user.id && r.can_authorize !== false).length
    : pendingItems.filter((r) => r.can_approve && r.requested_by !== me?.user.id).length
  const mine = single ? 0 : pendingItems.filter((r) => r.requested_by === me?.user.id).length

  if (!isMaster) {
    return (
      <>
        <PageHeader title="Aprovações financeiras" />
        <Card>
          <NoPermission
            permission="finance.approve"
            title="Exclusivo dos Admin Master"
            description="A aprovação de operações financeiras (PIX OUT, despesas, ajustes, conciliação) é feita somente pelos Administradores Master."
          />
        </Card>
      </>
    )
  }

  const columns: DataTableColumn<ApprovalRequest>[] = [
    {
      id: 'action',
      header: 'Operação',
      mobile: 'primary',
      cell: (r) => (
        <div className="flex flex-col gap-0.5">
          <span className="font-semibold first-letter:uppercase">{r.action_label}</span>
          <span className="line-clamp-1 text-xs text-text-muted">{r.reason}</span>
        </div>
      ),
    },
    { id: 'amount', header: 'Valor', numeric: true, mobile: 'field', cell: (r) => (r.amount_cents !== null ? formatCents(r.amount_cents) : EMPTY_VALUE) },
    // ÚNICA: quem solicita é sempre a própria pessoa — coluna redundante (e a tabela não cabe em 768 px).
    ...(single
      ? []
      : [{ id: 'requested_by', header: 'Solicitado por', mobile: 'field' as const, cell: (r: ApprovalRequest) => personLabel(r.requested_by_name, r.requested_by, me?.user.id) }]),
    { id: 'requested_at', header: 'Solicitado em', mobile: 'secondary', className: 'max-lg:hidden', cell: (r) => formatDateTime(r.requested_at) },
    { id: 'expires_at', header: 'Expira em', mobile: 'field', className: 'max-lg:hidden', cell: (r) => (r.status === 'PENDING' ? formatDateTime(r.expires_at) : EMPTY_VALUE) },
    {
      id: 'status',
      header: 'Situação',
      mobile: 'badge',
      cell: (r) => (
        <div className="flex flex-wrap gap-1.5">
          <StatusPill map={APPROVAL_STATUS} value={r.status} />
          {r.status === 'APPROVED' ? <StatusPill map={EXECUTION_STATE} value={r.execution_state} /> : null}
          {r.approval_mode === 'SINGLE' ? <Badge tone="outline">{approvalModeNoun('SINGLE')}</Badge> : null}
          {r.is_mode_change ? <Badge tone="warning">Muda o modo de aprovação</Badge> : null}
        </div>
      ),
    },
  ]

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Aprovações financeiras"
        description={
          single
            ? 'Aprovação única: você autoriza cada operação financeira com TOTP, senha financeira e código por e-mail.'
            : 'Dupla aprovação entre os dois Admin Master. Quem solicita não aprova.'
        }
      />
      <LiveStale updatedAt={list.dataUpdatedAt} onRefresh={() => void list.refetch()} refreshing={list.isFetching} />
      <FinanceSecurityNotice status={sec.data} loading={sec.isLoading} error={sec.error} onRetry={() => void sec.refetch()} />
      {awaitingMe > 0 ? (
        <Alert variant="warning">
          <BellRing aria-hidden="true" />
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <AlertTitle>
              {single
                ? awaitingMe === 1
                  ? '1 pedido aguarda a sua autorização'
                  : `${awaitingMe} pedidos aguardam a sua autorização`
                : awaitingMe === 1
                  ? '1 pedido aguarda a sua aprovação'
                  : `${awaitingMe} pedidos aguardam a sua aprovação`}
            </AlertTitle>
            <AlertDescription>
              {single
                ? 'Pedidos expiram automaticamente. Revise o conteúdo antes de autorizar ou cancelar.'
                : 'Pedidos expiram automaticamente. Revise o conteúdo antes de aprovar ou rejeitar.'}
            </AlertDescription>
          </div>
        </Alert>
      ) : null}
      {mine > 0 ? (
        <Alert variant="info">
          <ShieldCheck aria-hidden="true" />
          <AlertDescription>
            {mine === 1 ? 'Você tem 1 solicitação aguardando o outro Admin Master.' : `Você tem ${mine} solicitações aguardando o outro Admin Master.`}
          </AlertDescription>
        </Alert>
      ) : null}
      <Card>
        <DataTable
          caption="Pedidos de aprovação financeira"
          columns={columns}
          data={list.data}
          getRowId={(r) => r.id}
          controller={table}
          isLoading={list.isLoading}
          isFetching={list.isFetching}
          error={list.error}
          onRetry={() => void list.refetch()}
          filtersActive={status !== 'PENDING'}
          empty={{
            title: status === 'PENDING' ? 'Nenhum pedido pendente.' : 'Nenhum pedido nesta situação.',
            description: single
              ? 'Pedidos de PIX OUT, despesas, ajustes e conciliação aparecem aqui para a sua autorização.'
              : 'Solicitações de PIX OUT, despesas, ajustes e conciliação aparecem aqui para o outro Admin Master.',
            icon: ShieldCheck,
          }}
          toolbar={
            <div className="flex flex-col gap-2">
              <span id="approvals-filter" className="text-sm font-semibold text-text">
                Situação
              </span>
              <ToggleGroup
                type="single"
                value={status}
                onValueChange={(v) => v && table.setFilter('status', v === 'PENDING' ? null : v)}
                aria-labelledby="approvals-filter"
              >
                {FILTERS.map((f) => (
                  <ToggleGroupItem key={f.value} value={f.value}>
                    {f.label}
                  </ToggleGroupItem>
                ))}
              </ToggleGroup>
            </div>
          }
          rowActions={(r) => (
            <Button variant="secondary" onClick={() => setOpenId(r.id)} aria-label={`Revisar pedido: ${r.action_label}`}>
              {r.status === 'PENDING' && r.approval_mode === 'SINGLE' && r.requested_by === me?.user.id && r.can_authorize !== false
                ? 'Revisar e autorizar'
                : r.status === 'PENDING' && r.can_approve && r.requested_by !== me?.user.id
                  ? 'Revisar e decidir'
                  : 'Ver detalhes'}
            </Button>
          )}
        />
      </Card>
      <FinancialPasswordChangedAt status={sec.data} />
      <ApprovalDetailSheet
        requestId={openId}
        onOpenChange={(o) => !o && setOpenId(null)}
        meId={me?.user.id}
        ready={ready}
      />
    </div>
  )
}
