/** CRM → Propostas (P001 §107): lista com situação efetiva (vencida = 30 dias após o envio), total e vendedor. */
import { Link } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { FileText, PlusCircle } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Field, FieldLabel } from '@/components/ui/field'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { Can } from '@/components/auth/guards'
import { StatusPill } from '@/components/finance/status-pill'
import { PROPOSAL_STATUS } from '@/components/finance/labels'
import { formatIsoDate } from '@/components/finance/period'
import { personLabel } from '@/components/finance/names'
import { useTableState } from '@/hooks/useTableState'
import { useAuthStore } from '@/stores/authStore'
import { qk } from '@/lib/queryKeys'
import { formatBps, formatCents, formatDateTime } from '@/lib/format'
import { crmApi, type Proposal } from './api'

export default function ProposalsPage() {
  const meId = useAuthStore((s) => s.me?.user.id)
  const table = useTableState({ defaultPageSize: 25, filterKeys: ['status', 'seller'] })
  const params = {
    page: table.state.page,
    page_size: table.state.pageSize,
    status: table.state.filters.status,
    seller_id: table.state.filters.seller === 'me' ? meId : undefined,
  }
  const q = useQuery({ queryKey: qk.list('crm', { kind: 'proposals', ...params }), queryFn: () => crmApi.proposals(params), placeholderData: keepPreviousData })

  const columns: DataTableColumn<Proposal>[] = [
    {
      id: 'number',
      header: 'Proposta',
      mobile: 'primary',
      cell: (p) => (
        <div className="flex flex-col">
          <Link to={`/crm/propostas/${p.id}`} className="font-semibold text-link underline-offset-4 hover:underline">
            {p.number}
          </Link>
          <span className="text-xs text-text-muted">{p.title}</span>
        </div>
      ),
    },
    { id: 'status', header: 'Situação', mobile: 'badge', cell: (p) => <StatusPill map={PROPOSAL_STATUS} value={p.status} /> },
    { id: 'seller', header: 'Vendedor', mobile: 'field', cell: (p) => personLabel(p.seller_name, p.seller_id, meId) },
    { id: 'discount', header: 'Desconto', numeric: true, mobile: 'field', cell: (p) => formatBps(p.discount_bps) },
    { id: 'total', header: 'Total', numeric: true, mobile: 'field', cell: (p) => formatCents(p.total_cents) },
    { id: 'valid', header: 'Válida até', mobile: 'field', cell: (p) => (p.sent_at ? formatIsoDate(p.valid_until) : 'Após o envio (30 dias)') },
    { id: 'updated', header: 'Atualizada em', mobile: 'secondary', cell: (p) => formatDateTime(p.updated_at) },
  ]

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Propostas"
        description="Propostas comerciais com PDF e validade de 30 dias a partir do envio"
        actions={
          <Can permission="crm.manage">
            <Button asChild>
              <Link to="/crm/propostas/nova">
                <PlusCircle aria-hidden="true" />
                Nova proposta
              </Link>
            </Button>
          </Can>
        }
      />
      <Card>
        <DataTable
          caption="Propostas"
          columns={columns}
          data={q.data}
          getRowId={(p) => p.id}
          controller={table}
          isLoading={q.isLoading}
          isFetching={q.isFetching}
          error={q.error}
          onRetry={() => void q.refetch()}
          filtersActive={Object.keys(table.state.filters).length > 0}
          empty={{ title: 'Nenhuma proposta ainda.', description: 'Crie uma proposta a partir de um lead ou de uma empresa cliente.', icon: FileText }}
          toolbar={
            <>
              <Field className="w-full max-w-[260px]">
                <FieldLabel>Situação</FieldLabel>
                <Select value={table.state.filters.status ?? 'all'} onValueChange={(v) => table.setFilter('status', v === 'all' ? null : v)}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      <SelectItem value="all">Todas</SelectItem>
                      {Object.entries(PROPOSAL_STATUS).map(([k, v]) => (
                        <SelectItem key={k} value={k}>
                          {v.label}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  </SelectContent>
                </Select>
              </Field>
              <Field className="w-full max-w-[200px]">
                <FieldLabel>Vendedor</FieldLabel>
                <Select value={table.state.filters.seller ?? 'all'} onValueChange={(v) => table.setFilter('seller', v === 'all' ? null : v)}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      <SelectItem value="all">Todos</SelectItem>
                      <SelectItem value="me">Minhas propostas</SelectItem>
                    </SelectGroup>
                  </SelectContent>
                </Select>
              </Field>
            </>
          }
        />
      </Card>
    </div>
  )
}
