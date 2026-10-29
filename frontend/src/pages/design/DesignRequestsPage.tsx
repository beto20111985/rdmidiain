/**
 * Pedidos de arte — GET /design/requests (escopo no servidor). Mesma página para a RD
 * (/design/briefings), Minhas campanhas (/anunciante/design) e Minha unidade
 * (/unidade/design). Atualiza por evento WS `designer.updated` (domínio `design`).
 */
import { Link } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { ClipboardList, Plus } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Field, FieldLabel } from '@/components/ui/field'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { useTableState } from '@/hooks/useTableState'
import { useCan } from '@/hooks/usePermissions'
import { useActiveRouteContext } from '@/stores/contextStore'
import { http, type Paginated } from '@/lib/http'
import { qk } from '@/lib/queryKeys'
import { formatCents, formatDateTime } from '@/lib/format'
import { DESIGN_BILLING, DESIGN_KIND, DESIGN_STATUS, DESIGN_STATUS_STAFF, PAYMENT_STATUS, labelOf, lookup } from '@/components/admin/labels'
import { RealtimeStale, ToneBadge } from '@/components/admin/ui'
import { DESIGN_PATHS, revisionLimit } from './paths'
import type { DesignRequest } from './types'

const VERSION_STATUSES = ['EM_REVISAO', 'ALTERACAO_SOLICITADA', 'APROVADA', 'PUBLICADA'] as const

export function DesignRequestsList({ mode = 'all' }: { mode?: 'all' | 'versions' }) {
  const ctx = useActiveRouteContext() ?? 'rd'
  const paths = DESIGN_PATHS[ctx]
  const staff = ctx === 'rd'
  const can = useCan()
  const table = useTableState({ defaultPageSize: 25, filterKeys: ['status'] })
  const status = table.state.filters.status ?? (mode === 'versions' ? 'EM_REVISAO' : undefined)
  const params = { page: table.state.page, page_size: table.state.pageSize, status }
  const q = useQuery({
    queryKey: qk.list('design', params),
    queryFn: () => http.get<Paginated<DesignRequest>>('/design/requests', { query: params }),
    placeholderData: keepPreviousData,
  })
  const statusMap = staff ? DESIGN_STATUS_STAFF : DESIGN_STATUS

  const columns: DataTableColumn<DesignRequest>[] = [
    {
      id: 'title',
      header: 'Pedido',
      mobile: 'primary',
      cell: (r) => (
        <div className="flex min-w-0 flex-col">
          <Link to={paths.detail(r.id)} className="font-semibold text-link underline-offset-4 hover:underline">
            {r.code} · {r.title}
          </Link>
          <span className="text-xs text-text-muted">
            {labelOf(DESIGN_KIND, r.kind)} · {r.media_type === 'VIDEO' ? `Vídeo ${r.duration_s}s` : 'Imagem'} · {r.orientation === 'VERTICAL' ? '1080×1920' : '1920×1080'}
          </span>
        </div>
      ),
    },
    { id: 'status', header: 'Situação', mobile: 'badge', cell: (r) => <ToneBadge value={lookup(statusMap, r.status)} /> },
    {
      id: 'rev',
      header: 'Revisões',
      mobile: 'field',
      cell: (r) => (
        <span className="tabular-nums">
          {r.revisions_used} de {revisionLimit(r)}
          {r.extra_revisions_count ? <span className="text-xs text-text-muted"> (+{r.extra_revisions_count} extra)</span> : null}
        </span>
      ),
    },
    {
      id: 'billing',
      header: 'Cobrança',
      mobile: 'field',
      cell: (r) => (
        <span className="flex flex-col">
          <span>
            {labelOf(DESIGN_BILLING, r.billing)}
            {r.billing === 'COBRAR' ? ` · ${formatCents(r.price_cents)}` : ''}
          </span>
          {r.payment_status !== 'NAO_APLICAVEL' ? <span className="text-xs text-text-muted">{lookup(PAYMENT_STATUS, r.payment_status).label}</span> : null}
        </span>
      ),
    },
    { id: 'due', header: 'Prazo', mobile: 'field', cell: (r) => <span className="tabular-nums">{formatDateTime(r.due_at)}</span> },
    { id: 'updated', header: 'Atualizado', mobile: 'secondary', cell: (r) => <span className="tabular-nums">{formatDateTime(r.updated_at ?? r.created_at)}</span> },
  ]

  const canCreate = can('design.request') || can('design.work')
  const action = canCreate ? (
    <Button asChild>
      <Link to={paths.new}>
        <Plus aria-hidden="true" />
        Novo pedido de arte
      </Link>
    </Button>
  ) : undefined

  return (
    <>
      <PageHeader
        title={mode === 'versions' ? 'Versões e revisões' : staff ? 'Briefings' : 'Design'}
        description={
          mode === 'versions'
            ? 'Versões entregues, uso das 3 revisões incluídas e aprovação formal do cliente.'
            : staff
              ? 'Pedidos de criação de arte com briefing do cliente.'
              : 'Peça a criação da arte à RD (serviço pago à parte), acompanhe as versões e aprove.'
        }
        actions={mode === 'all' ? action : undefined}
      />
      <div className="flex flex-col gap-4">
        <RealtimeStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />
        <Card>
          <DataTable
            caption="Pedidos de arte"
            columns={columns}
            data={q.data}
            getRowId={(r) => r.id}
            controller={table}
            isLoading={q.isLoading}
            isFetching={q.isFetching}
            error={q.error}
            onRetry={() => void q.refetch()}
            filtersActive={mode === 'all' && !!table.state.filters.status}
            empty={{
              icon: ClipboardList,
              title: mode === 'versions' ? 'Nenhum pedido nesta etapa.' : 'Nenhum pedido de arte ainda.',
              description: mode === 'versions' ? 'Escolha outra etapa acima.' : 'Envie um briefing com logotipo, textos e referências para a RD criar a sua arte.',
              action: mode === 'all' ? action : undefined,
            }}
            toolbar={
              mode === 'versions' ? (
                <ToggleGroup
                  type="single"
                  aria-label="Etapa"
                  value={status}
                  onValueChange={(v) => v && table.setFilter('status', v === 'EM_REVISAO' ? null : v)}
                >
                  {VERSION_STATUSES.map((s) => (
                    <ToggleGroupItem key={s} value={s}>
                      {lookup(DESIGN_STATUS_STAFF, s).label}
                    </ToggleGroupItem>
                  ))}
                </ToggleGroup>
              ) : (
                <Field className="w-full sm:w-64">
                  <FieldLabel>Situação</FieldLabel>
                  <Select value={table.state.filters.status ?? 'all'} onValueChange={(v) => table.setFilter('status', v === 'all' ? null : v)}>
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectGroup>
                        <SelectItem value="all">Todas</SelectItem>
                        {Object.entries(statusMap).map(([k, v]) => (
                          <SelectItem key={k} value={k}>
                            {v.label}
                          </SelectItem>
                        ))}
                      </SelectGroup>
                    </SelectContent>
                  </Select>
                </Field>
              )
            }
          />
        </Card>
      </div>
    </>
  )
}

export default function DesignRequestsPage() {
  return <DesignRequestsList mode="all" />
}
