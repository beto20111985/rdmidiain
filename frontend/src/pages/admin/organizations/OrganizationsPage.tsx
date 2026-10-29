/**
 * Empresas (RD) — GET /organizations: papéis explícitos (anunciante, parceiro, agência,
 * franquia — combináveis), PF/PJ, KYC e situação. Filtros na URL (preservados ao voltar).
 */
import { Link } from 'react-router-dom'
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { Building2, Plus } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Field, FieldLabel } from '@/components/ui/field'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { Can } from '@/components/auth/guards'
import { useTableState } from '@/hooks/useTableState'
import { useRealtimeEvent } from '@/lib/realtime/context'
import { http, type Paginated } from '@/lib/http'
import { qk } from '@/lib/queryKeys'
import { BUSINESS_ROLES, BUSINESS_ROLE_LABELS, KYC, lookup } from '@/components/admin/labels'
import { RealtimeStale, ToneBadge } from '@/components/admin/ui'
import { useTableSearch } from '@/components/admin/hooks'
import type { Organization } from '../types'

export default function OrganizationsPage() {
  const table = useTableState({ defaultPageSize: 25, defaultSort: 'name', filterKeys: ['role', 'is_active'] })
  const qc = useQueryClient()
  const [search, setSearch] = useTableSearch(table)

  const params = {
    page: table.state.page,
    page_size: table.state.pageSize,
    sort: table.state.sort ?? 'name',
    search: table.state.q || undefined,
    role: table.state.filters.role,
    is_active: table.state.filters.is_active,
  }
  const q = useQuery({
    queryKey: qk.list('organizations', params),
    queryFn: () => http.get<Paginated<Organization>>('/organizations', { query: params }),
    placeholderData: keepPreviousData,
  })

  // `organization.updated` (W-ORG) não está no mapa padrão: invalida só este domínio.
  useRealtimeEvent('*', (e) => {
    if (e.type === 'organization.updated') void qc.invalidateQueries({ queryKey: ['organizations'] })
  })

  const columns: DataTableColumn<Organization>[] = [
    {
      id: 'name',
      header: 'Empresa',
      sortField: 'name',
      mobile: 'primary',
      cell: (o) => (
        <div className="flex min-w-0 flex-col">
          <Link to={`/empresas/${o.id}`} className="truncate font-semibold text-link underline-offset-4 hover:underline">
            {o.trade_name || o.name}
          </Link>
          {o.trade_name ? <span className="truncate text-xs text-text-muted">{o.name}</span> : null}
        </div>
      ),
    },
    {
      id: 'document',
      header: 'CPF/CNPJ',
      mobile: 'secondary',
      cell: (o) => (
        <span className="tabular-nums">
          {o.document_formatted} <span className="text-xs text-text-muted">({o.person_type})</span>
        </span>
      ),
    },
    {
      id: 'roles',
      header: 'Papéis',
      mobile: 'field',
      cell: (o) => (
        <div className="flex flex-wrap gap-1">
          {o.business_roles.map((r) => (
            <Badge key={r} tone="outline">
              {BUSINESS_ROLE_LABELS[r as keyof typeof BUSINESS_ROLE_LABELS] ?? r}
            </Badge>
          ))}
        </div>
      ),
    },
    { id: 'city', header: 'Cidade/UF', mobile: 'field', cell: (o) => (o.city ? `${o.city}/${o.state ?? ''}` : '—') },
    { id: 'kyc', header: 'KYC', mobile: 'field', cell: (o) => <ToneBadge value={lookup(KYC, o.kyc_status)} /> },
    {
      id: 'status',
      header: 'Situação',
      mobile: 'badge',
      cell: (o) => <ToneBadge value={o.is_active ? { label: 'Ativa', tone: 'success' } : { label: 'Inativa', tone: 'neutral' }} />,
    },
  ]

  const action = (
    <Can permission="organizations.manage">
      <Button asChild>
        <Link to="/empresas/nova">
          <Plus aria-hidden="true" />
          Nova empresa
        </Link>
      </Button>
    </Can>
  )

  return (
    <>
      <PageHeader
        title="Empresas"
        description={q.data ? `${q.data.total} empresa(s) no seu acesso` : undefined}
        actions={action}
      />
      <div className="flex flex-col gap-4">
        <RealtimeStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />
        <Card>
          <DataTable
            caption="Empresas"
            columns={columns}
            data={q.data}
            getRowId={(o) => o.id}
            controller={table}
            isLoading={q.isLoading}
            isFetching={q.isFetching}
            error={q.error}
            onRetry={() => void q.refetch()}
            filtersActive={!!table.state.q || Object.keys(table.state.filters).length > 0}
            empty={{
              icon: Building2,
              title: 'Nenhuma empresa cadastrada ainda.',
              description: 'Cadastre anunciantes, parceiros, agências e franquias para usar nas campanhas e estabelecimentos.',
              action: action,
            }}
            toolbar={
              <>
                <Field className="w-full sm:max-w-xs">
                  <FieldLabel>Buscar</FieldLabel>
                  <Input type="search" placeholder="Nome ou CPF/CNPJ" value={search} onChange={(e) => setSearch(e.target.value)} />
                </Field>
                <Field className="w-full sm:w-56">
                  <FieldLabel>Papel</FieldLabel>
                  <Select value={table.state.filters.role ?? 'all'} onValueChange={(v) => table.setFilter('role', v === 'all' ? null : v)}>
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectGroup>
                        <SelectItem value="all">Todos os papéis</SelectItem>
                        {BUSINESS_ROLES.map((r) => (
                          <SelectItem key={r} value={r}>
                            {BUSINESS_ROLE_LABELS[r]}
                          </SelectItem>
                        ))}
                      </SelectGroup>
                    </SelectContent>
                  </Select>
                </Field>
                <Field className="w-full sm:w-44">
                  <FieldLabel>Situação</FieldLabel>
                  <Select value={table.state.filters.is_active ?? 'all'} onValueChange={(v) => table.setFilter('is_active', v === 'all' ? null : v)}>
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectGroup>
                        <SelectItem value="all">Todas</SelectItem>
                        <SelectItem value="true">Ativas</SelectItem>
                        <SelectItem value="false">Inativas</SelectItem>
                      </SelectGroup>
                    </SelectContent>
                  </Select>
                </Field>
              </>
            }
          />
        </Card>
      </div>
    </>
  )
}
