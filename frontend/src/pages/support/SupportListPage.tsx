/**
 * Suporte — lista de chamados (GET /support/tickets), filtrada pelo escopo no servidor.
 * Mesma página para RD (/suporte), Minha unidade (/unidade/suporte) e Minhas campanhas
 * (/anunciante/suporte). Equipe com `support.manage` tem a busca EXATA por código RD.
 * `?novo=1&categoria=…&codigo=…&tela=…` (link do FE-OPS) abre o formulário pré-preenchido.
 */
import { Link, Navigate, useLocation } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { LifeBuoy, Plus } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Checkbox } from '@/components/ui/checkbox'
import { Field, FieldError, FieldLabel } from '@/components/ui/field'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { useTableState } from '@/hooks/useTableState'
import { useCan } from '@/hooks/usePermissions'
import { useActiveRouteContext } from '@/stores/contextStore'
import { http, type Paginated } from '@/lib/http'
import { qk } from '@/lib/queryKeys'
import { formatDateTime } from '@/lib/format'
import { TICKET_CATEGORY, TICKET_PRIORITY, TICKET_STATUS, labelOf, lookup } from '@/components/admin/labels'
import { RealtimeStale, ToneBadge } from '@/components/admin/ui'
import { CREATIVE_CODE_RE, SUPPORT_BASE, normalizeCreativeCode } from '@/components/admin/support-links'
import { CreativeLookupCard } from './CreativeLookupCard'
import type { Ticket } from './types'

export default function SupportListPage() {
  const location = useLocation()
  const ctx = useActiveRouteContext() ?? 'rd'
  const base = SUPPORT_BASE[ctx]
  const can = useCan()
  const table = useTableState({ defaultPageSize: 25, filterKeys: ['status', 'category', 'meus', 'codigo'] })
  const f = table.state.filters

  const sp = new URLSearchParams(location.search)
  const wantsNew = sp.get('novo') === '1'
  const codeNorm = normalizeCreativeCode(f.codigo)
  const codeInvalid = !!codeNorm && !CREATIVE_CODE_RE.test(codeNorm)
  const codeFilter = codeNorm && !codeInvalid ? codeNorm : undefined
  const params = {
    page: table.state.page,
    page_size: table.state.pageSize,
    status: f.status,
    category: f.category,
    assigned_to_me: f.meus === '1' ? true : undefined,
    creative_code: codeFilter,
  }
  const q = useQuery({
    queryKey: qk.list('support', params),
    queryFn: () => http.get<Paginated<Ticket>>('/support/tickets', { query: params }),
    placeholderData: keepPreviousData,
    enabled: !wantsNew,
  })

  if (wantsNew) {
    sp.delete('novo')
    const qs = sp.toString()
    return <Navigate to={`${base}/novo${qs ? `?${qs}` : ''}`} replace />
  }

  const staffTools = ctx === 'rd' && can('support.manage')
  const columns: DataTableColumn<Ticket>[] = [
    {
      id: 'number',
      header: 'Chamado',
      mobile: 'primary',
      cell: (t) => (
        <div className="flex min-w-0 flex-col">
          <Link to={`${base}/${t.id}`} className="font-semibold text-link underline-offset-4 hover:underline">
            {t.number} · {t.title}
          </Link>
          <span className="text-xs text-text-muted">{labelOf(TICKET_CATEGORY, t.category)}</span>
        </div>
      ),
    },
    { id: 'code', header: 'Anúncio', mobile: 'field', cell: (t) => (t.creative_code ? <code className="font-mono">{t.creative_code}</code> : '—') },
    { id: 'status', header: 'Situação', mobile: 'badge', cell: (t) => <ToneBadge value={lookup(TICKET_STATUS, t.status)} /> },
    { id: 'priority', header: 'Prioridade', mobile: 'field', cell: (t) => <ToneBadge value={lookup(TICKET_PRIORITY, t.priority)} /> },
    {
      id: 'sla',
      header: 'Prazo (SLA)',
      mobile: 'field',
      cell: (t) =>
        t.sla_breached ? (
          <Badge tone="danger">Vencido em {formatDateTime(t.sla_due_at)}</Badge>
        ) : (
          <span className="tabular-nums">{formatDateTime(t.sla_due_at)}</span>
        ),
    },
    { id: 'created', header: 'Aberto em', mobile: 'secondary', cell: (t) => <span className="tabular-nums">{formatDateTime(t.created_at)}</span> },
  ]

  const action = (
    <Button asChild>
      <Link to={`${base}/novo`}>
        <Plus aria-hidden="true" />
        Abrir chamado
      </Link>
    </Button>
  )

  return (
    <>
      <PageHeader
        title="Suporte"
        description={ctx === 'establishment' ? 'Chamados da sua unidade — inclusive pedidos de retirada de anúncio pelo código exibido na tela.' : undefined}
        actions={action}
      />
      <div className="flex flex-col gap-4 xl:gap-6">
        {staffTools ? <CreativeLookupCard /> : null}
        <RealtimeStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />
        <Card>
          <DataTable
            caption="Chamados"
            columns={columns}
            data={q.data}
            getRowId={(t) => t.id}
            controller={table}
            isLoading={q.isLoading}
            isFetching={q.isFetching}
            error={q.error}
            onRetry={() => void q.refetch()}
            filtersActive={Object.keys(f).length > 0}
            empty={{ icon: LifeBuoy, title: 'Nenhum chamado aberto.', description: 'Abra um chamado para falar com a equipe RD.', action }}
            toolbar={
              <>
                <Field className="w-full sm:w-52">
                  <FieldLabel>Situação</FieldLabel>
                  <Select value={f.status ?? 'all'} onValueChange={(v) => table.setFilter('status', v === 'all' ? null : v)}>
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectGroup>
                        <SelectItem value="all">Todas</SelectItem>
                        {Object.entries(TICKET_STATUS).map(([k, v]) => (
                          <SelectItem key={k} value={k}>
                            {v.label}
                          </SelectItem>
                        ))}
                      </SelectGroup>
                    </SelectContent>
                  </Select>
                </Field>
                <Field className="w-full sm:w-52">
                  <FieldLabel>Assunto</FieldLabel>
                  <Select value={f.category ?? 'all'} onValueChange={(v) => table.setFilter('category', v === 'all' ? null : v)}>
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectGroup>
                        <SelectItem value="all">Todos</SelectItem>
                        {Object.entries(TICKET_CATEGORY).map(([k, v]) => (
                          <SelectItem key={k} value={k}>
                            {v}
                          </SelectItem>
                        ))}
                      </SelectGroup>
                    </SelectContent>
                  </Select>
                </Field>
                <Field className="w-full sm:w-44" invalid={codeInvalid}>
                  <FieldLabel>Código do anúncio</FieldLabel>
                  <Input
                    defaultValue={f.codigo ?? ''}
                    placeholder="RD-XXXXXX"
                    className="font-mono"
                    onBlur={(e) => table.setFilter('codigo', e.target.value.trim() || null)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') table.setFilter('codigo', (e.target as HTMLInputElement).value.trim() || null)
                    }}
                  />
                  <FieldError>{codeInvalid ? 'Formato RD-XXXXXX.' : undefined}</FieldError>
                </Field>
                {ctx === 'rd' && can('support.manage') ? (
                  <Field orientation="horizontal" className="min-h-11 w-auto">
                    <Checkbox checked={f.meus === '1'} onCheckedChange={(c) => table.setFilter('meus', c === true ? '1' : null)} />
                    <FieldLabel className="font-normal">Atribuídos a mim</FieldLabel>
                  </Field>
                ) : null}
              </>
            }
          />
        </Card>
      </div>
    </>
  )
}
