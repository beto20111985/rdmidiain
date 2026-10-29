/**
 * Estabelecimentos (RD/equipe): lista paginada com filtros (UF, cidade, região, categoria,
 * status, contrato, plano do parceiro). Backend: GET /establishments (W-ORG), escopo pelo servidor.
 */
import * as React from 'react'
import { Link } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Plus, Store, UserPlus } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Can } from '@/components/auth/guards'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Field, FieldLabel } from '@/components/ui/field'
import { Badge } from '@/components/ui/badge'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { NoPermission } from '@/components/patterns/states'
import { RealtimeStale } from '@/components/ops/realtime-stale'
import { FilterSelect } from '@/components/ops/filter-select'
import { useTableState } from '@/hooks/useTableState'
import { qk } from '@/lib/queryKeys'
import { isApiError } from '@/lib/errors'
import { formatNumber } from '@/lib/format'
import { treeOrder } from '@/lib/tree'
import { establishmentsApi } from '@/pages/ops/api/client'
import { usePartnerPlans, useTaxonomyNodes } from '@/pages/ops/api/hooks'
import { ONBOARDING_STATUS_EST_LABEL, REGION_LABEL, UFS, labelOf } from '@/pages/ops/api/labels'
import type { Establishment } from '@/pages/ops/api/types'

const FILTERS = ['state', 'city', 'region', 'category_id', 'is_active', 'onboarding_status', 'partner_plan_id']

export default function EstablishmentsListPage() {
  const table = useTableState({ defaultPageSize: 25, defaultSort: 'name', filterKeys: FILTERS })
  const f = table.state.filters
  const nodes = useTaxonomyNodes()
  const plans = usePartnerPlans(true)
  const [cityDraft, setCityDraft] = React.useState(f.city ?? '')

  React.useEffect(() => setCityDraft(f.city ?? ''), [f.city])

  const apiParams = React.useMemo(() => {
    const { q, city, ...rest } = table.apiParams as Record<string, string | number | undefined>
    return {
      ...rest,
      search: q || undefined,
      city_key: city && f.state ? `${f.state}:${city}` : undefined,
    }
  }, [table.apiParams, f.state])

  const q = useQuery({
    queryKey: qk.list('establishments', apiParams),
    queryFn: () => establishmentsApi.list(apiParams),
    placeholderData: keepPreviousData,
  })

  const nodeName = React.useMemo(() => new Map((nodes.data?.items ?? []).map((n) => [n.id, n.name])), [nodes.data])
  const planName = React.useMemo(() => new Map((plans.data?.items ?? []).map((p) => [p.id, p.name])), [plans.data])

  if (isApiError(q.error) && q.error.isForbidden) return <NoPermission permission="establishments.view" />

  const columns: DataTableColumn<Establishment>[] = [
    {
      id: 'name',
      header: 'Estabelecimento',
      sortField: 'name',
      mobile: 'primary',
      cell: (e) => (
        <div className="flex min-w-0 flex-col">
          <Link to={`/estabelecimentos/${e.id}`} className="font-semibold text-link underline-offset-4 hover:underline">
            {e.name}
          </Link>
          {e.trade_name && e.trade_name !== e.name ? <span className="text-xs text-text-muted">{e.trade_name}</span> : null}
        </div>
      ),
    },
    {
      id: 'city',
      header: 'Cidade/UF',
      sortField: 'city',
      mobile: 'secondary',
      cell: (e) => (
        <span>
          {e.city}/{e.state}
          <span className="block text-xs text-text-muted">{e.region_label ?? labelOf(REGION_LABEL, e.region)}</span>
        </span>
      ),
    },
    { id: 'category', header: 'Categoria', cell: (e) => (e.category_id ? (nodeName.get(e.category_id) ?? '—') : '—') },
    { id: 'plan', header: 'Plano do parceiro', cell: (e) => (e.partner_plan_id ? (planName.get(e.partner_plan_id) ?? '—') : 'Sem plano') },
    {
      id: 'contract',
      header: 'Contrato',
      cell: (e) => (
        <Badge tone={e.onboarding_status === 'ACTIVE' ? 'success' : 'warning'}>
          {labelOf(ONBOARDING_STATUS_EST_LABEL, e.onboarding_status)}
        </Badge>
      ),
    },
    {
      id: 'status',
      header: 'Status',
      mobile: 'badge',
      cell: (e) => <Badge tone={e.is_active ? 'success' : 'neutral'}>{e.is_active ? 'Ativo' : 'Inativo'}</Badge>,
    },
    { id: 'tz', header: 'Fuso', mobile: 'hidden', cell: (e) => <span className="text-xs">{e.timezone}</span> },
  ]

  const filtersActive = !!table.state.q || Object.keys(f).length > 0

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Estabelecimentos"
        description={q.data ? `${formatNumber(q.data.total)} estabelecimento(s) no seu acesso` : undefined}
        actions={
          <Can permission="establishments.manage">
            <div className="flex flex-wrap gap-2">
              <Button asChild variant="secondary">
                <Link to="/estabelecimentos/cadastros">
                  <UserPlus aria-hidden="true" />
                  Cadastros de parceiros
                </Link>
              </Button>
              <Button asChild>
                <Link to="/estabelecimentos/novo">
                  <Plus aria-hidden="true" />
                  Adicionar estabelecimento
                </Link>
              </Button>
            </div>
          </Can>
        }
      />
      <RealtimeStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />
      <Card>
        <DataTable
          caption="Estabelecimentos"
          columns={columns}
          data={q.data}
          getRowId={(e) => e.id}
          controller={table}
          isLoading={q.isLoading}
          isFetching={q.isFetching}
          error={q.error}
          onRetry={() => void q.refetch()}
          filtersActive={filtersActive}
          empty={{
            icon: Store,
            title: 'Nenhum estabelecimento cadastrado no seu acesso.',
            description: 'Cadastre um estabelecimento parceiro ou convide o responsável para o auto-cadastro.',
            action: (
              <Can permission="establishments.manage">
                <Button asChild>
                  <Link to="/estabelecimentos/novo">Adicionar estabelecimento</Link>
                </Button>
              </Can>
            ),
          }}
          toolbar={
            <>
              <Field className="w-full sm:max-w-xs">
                <FieldLabel>Buscar</FieldLabel>
                <Input value={table.state.q} onChange={(e) => table.setQuery(e.target.value)} placeholder="Nome, nome fantasia ou cidade" />
              </Field>
              <FilterSelect
                label="UF"
                value={f.state}
                onChange={(v) => {
                  table.setFilter('state', v)
                  if (!v) table.setFilter('city', null)
                }}
                options={UFS.map((u) => ({ value: u.uf, label: `${u.uf} — ${u.name}` }))}
                className="sm:w-40"
              />
              <Field className="w-full sm:w-48">
                <FieldLabel>Cidade</FieldLabel>
                <Input
                  value={cityDraft}
                  disabled={!f.state}
                  placeholder={f.state ? 'Nome da cidade' : 'Escolha a UF'}
                  onChange={(e) => setCityDraft(e.target.value)}
                  onBlur={() => table.setFilter('city', cityDraft.trim() || null)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') table.setFilter('city', cityDraft.trim() || null)
                  }}
                />
              </Field>
              <FilterSelect
                label="Região"
                value={f.region}
                onChange={(v) => table.setFilter('region', v)}
                options={Object.entries(REGION_LABEL).map(([value, label]) => ({ value, label }))}
                allLabel="Todas"
              />
              <FilterSelect
                label="Categoria"
                value={f.category_id}
                onChange={(v) => table.setFilter('category_id', v)}
                options={treeOrder(nodes.data?.items ?? []).map((n) => ({ value: n.id, label: `${'— '.repeat(n.depth)}${n.name}` }))}
                allLabel="Todas"
              />
              <FilterSelect
                label="Status"
                value={f.is_active}
                onChange={(v) => table.setFilter('is_active', v)}
                options={[
                  { value: 'true', label: 'Ativos' },
                  { value: 'false', label: 'Inativos' },
                ]}
              />
              <FilterSelect
                label="Contrato"
                value={f.onboarding_status}
                onChange={(v) => table.setFilter('onboarding_status', v)}
                options={Object.entries(ONBOARDING_STATUS_EST_LABEL).map(([value, label]) => ({ value, label }))}
              />
              <FilterSelect
                label="Plano do parceiro"
                value={f.partner_plan_id}
                onChange={(v) => table.setFilter('partner_plan_id', v)}
                options={(plans.data?.items ?? []).map((p) => ({ value: p.id, label: p.name }))}
              />
            </>
          }
        />
      </Card>
    </div>
  )
}
