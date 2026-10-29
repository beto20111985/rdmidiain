/**
 * Estoque e patrimônio: TVs, TV Box, cabos, controles, suportes e acessórios (itens com nº de série
 * ou por quantidade), situação derivada do último local e resumo por tipo. Estoque ≠ inventário
 * publicitário. Backend: GET /equipment, GET /equipment/summary (W-ORG).
 */
import * as React from 'react'
import { Link } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { PackagePlus, Package } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Can } from '@/components/auth/guards'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Field, FieldLabel } from '@/components/ui/field'
import { Skeleton } from '@/components/ui/skeleton'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { ErrorState, NoPermission } from '@/components/patterns/states'
import { FilterSelect } from '@/components/ops/filter-select'
import { LocationLabel } from '@/components/ops/location-label'
import { RealtimeStale } from '@/components/ops/realtime-stale'
import { useTableState } from '@/hooks/useTableState'
import { isApiError } from '@/lib/errors'
import { qk } from '@/lib/queryKeys'
import { formatDateTime, formatNumber } from '@/lib/format'
import { equipmentApi } from '@/pages/ops/api/client'
import { EQUIPMENT_STATUS_LABEL, EQUIPMENT_TYPE_LABEL, LOCATION_KIND_LABEL, labelOf } from '@/pages/ops/api/labels'
import type { EquipmentItem } from '@/pages/ops/api/types'

const FILTERS = ['type', 'tracking', 'status', 'location_kind']

function Summary() {
  const q = useQuery({ queryKey: qk.sub('equipment', 'summary'), queryFn: equipmentApi.summary })
  if (q.isLoading) return <Skeleton className="h-28 w-full rounded-xl" />
  if (q.error) return <ErrorState error={q.error} onRetry={() => void q.refetch()} size="compact" />
  const serial = q.data?.serial_by_type_status ?? {}
  const types = Object.keys(serial)
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Resumo do estoque</CardTitle>
          <CardDescription>Itens com nº de série por situação; itens por quantidade com saldo nos estoques do seu acesso.</CardDescription>
        </div>
      </CardHeader>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <table className="w-full text-sm">
          <caption className="sr-only">Itens com número de série por tipo e situação</caption>
          <thead>
            <tr className="text-left text-xs text-text-muted">
              <th scope="col" className="py-1 font-semibold">Tipo</th>
              <th scope="col" className="py-1 text-right font-semibold">Em estoque</th>
              <th scope="col" className="py-1 text-right font-semibold">Instalados</th>
              <th scope="col" className="py-1 text-right font-semibold">Outros</th>
            </tr>
          </thead>
          <tbody>
            {types.length ? (
              types.map((t) => {
                const s = serial[t]
                const total = Object.values(s).reduce((a, b) => a + b, 0)
                const other = total - (s.IN_STOCK ?? 0) - (s.INSTALLED ?? 0)
                return (
                  <tr key={t} className="border-t border-border">
                    <th scope="row" className="py-2 text-left font-normal text-text">{labelOf(EQUIPMENT_TYPE_LABEL, t)}</th>
                    <td className="py-2 text-right tabular-nums">{formatNumber(s.IN_STOCK ?? 0)}</td>
                    <td className="py-2 text-right tabular-nums">{formatNumber(s.INSTALLED ?? 0)}</td>
                    <td className="py-2 text-right tabular-nums">{formatNumber(other)}</td>
                  </tr>
                )
              })
            ) : (
              <tr>
                <td colSpan={4} className="py-2 text-text-muted">Nenhum item com número de série.</td>
              </tr>
            )}
          </tbody>
        </table>
        <ul className="m-0 flex list-none flex-col divide-y divide-border p-0 text-sm" aria-label="Itens por quantidade no estoque">
          {(q.data?.bulk_in_warehouse ?? []).length ? (
            q.data!.bulk_in_warehouse.map((b) => (
              <li key={`${b.type}-${b.name}`} className="flex justify-between gap-2 py-2">
                <span className="text-text">{b.name} <span className="text-xs text-text-muted">({labelOf(EQUIPMENT_TYPE_LABEL, b.type)})</span></span>
                <span className="tabular-nums">{formatNumber(b.quantity)}</span>
              </li>
            ))
          ) : (
            <li className="py-2 text-text-muted">Nenhum item por quantidade.</li>
          )}
        </ul>
      </div>
    </Card>
  )
}

export default function EquipmentListPage() {
  const table = useTableState({ defaultPageSize: 25, filterKeys: FILTERS })
  const f = table.state.filters
  const params = React.useMemo(() => {
    const { q, sort: _s, ...rest } = table.apiParams as Record<string, string | number | undefined>
    return { ...rest, search: q || undefined }
  }, [table.apiParams])
  const q = useQuery({ queryKey: qk.list('equipment', params), queryFn: () => equipmentApi.list(params), placeholderData: keepPreviousData })

  if (isApiError(q.error) && q.error.isForbidden) return <NoPermission permission="equipment.view" />

  const columns: DataTableColumn<EquipmentItem>[] = [
    {
      id: 'name',
      header: 'Item',
      mobile: 'primary',
      cell: (i) => (
        <div className="flex min-w-0 flex-col">
          <Link to={`/estoque/${i.id}`} className="font-semibold text-link underline-offset-4 hover:underline">
            {i.name}
          </Link>
          <span className="text-xs text-text-muted">{[i.brand, i.model].filter(Boolean).join(' ') || labelOf(EQUIPMENT_TYPE_LABEL, i.type)}</span>
        </div>
      ),
    },
    { id: 'type', header: 'Tipo', mobile: 'secondary', cell: (i) => labelOf(EQUIPMENT_TYPE_LABEL, i.type) },
    {
      id: 'id',
      header: 'Série / patrimônio',
      cell: (i) =>
        i.tracking === 'BULK' ? (
          <span className="text-text-muted">Por quantidade ({i.unit})</span>
        ) : (
          <span className="tabular-nums">{[i.serial_number, i.asset_tag].filter(Boolean).join(' · ') || '—'}</span>
        ),
    },
    {
      id: 'status',
      header: 'Situação',
      mobile: 'badge',
      cell: (i) => (
        <Badge tone={i.status === 'IN_STOCK' ? 'success' : i.status === 'WRITTEN_OFF' ? 'neutral' : i.status === 'IN_MAINTENANCE' || i.status === 'IN_RMA' ? 'warning' : 'info'}>
          {labelOf(EQUIPMENT_STATUS_LABEL, i.status)}
        </Badge>
      ),
    },
    {
      id: 'location',
      header: 'Local',
      cell: (i) =>
        i.location ? (
          <LocationLabel
            kind={i.location.kind}
            establishmentId={i.location.establishment_id}
            screenId={i.location.screen_id}
            userId={i.location.user_id}
            cityKey={i.location.city_key}
          />
        ) : (
          <span className="text-text-muted">Ver saldos no detalhe</span>
        ),
    },
    { id: 'last', header: 'Última movimentação', mobile: 'hidden', cell: (i) => formatDateTime(i.last_movement_at) },
  ]

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Estoque e patrimônio"
        description="Equipamentos físicos da RD. Não confundir com o inventário publicitário (tempo de tela à venda)."
        actions={
          <Can permission="equipment.manage">
            <Button asChild>
              <Link to="/estoque/novo">
                <PackagePlus aria-hidden="true" />
                Entrada no estoque
              </Link>
            </Button>
          </Can>
        }
      />
      <RealtimeStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />
      <Summary />
      <Card>
        <DataTable
          caption="Itens do estoque"
          columns={columns}
          data={q.data}
          getRowId={(i) => i.id}
          controller={table}
          isLoading={q.isLoading}
          isFetching={q.isFetching}
          error={q.error}
          onRetry={() => void q.refetch()}
          filtersActive={!!table.state.q || Object.keys(f).length > 0}
          empty={{
            icon: Package,
            title: 'Nenhum item no estoque.',
            description: 'Registre a entrada de TVs, TV Box, cabos e acessórios para controlar instalação, comodato e manutenção.',
          }}
          toolbar={
            <>
              <Field className="w-full sm:max-w-xs">
                <FieldLabel>Buscar</FieldLabel>
                <Input value={table.state.q} onChange={(e) => table.setQuery(e.target.value)} placeholder="Nome, série, patrimônio, modelo" />
              </Field>
              <FilterSelect label="Tipo" value={f.type} onChange={(v) => table.setFilter('type', v)} options={Object.entries(EQUIPMENT_TYPE_LABEL).map(([value, label]) => ({ value, label }))} />
              <FilterSelect
                label="Rastreio"
                value={f.tracking}
                onChange={(v) => table.setFilter('tracking', v)}
                options={[
                  { value: 'SERIAL', label: 'Nº de série' },
                  { value: 'BULK', label: 'Quantidade' },
                ]}
              />
              <FilterSelect label="Situação" value={f.status} onChange={(v) => table.setFilter('status', v)} options={Object.entries(EQUIPMENT_STATUS_LABEL).map(([value, label]) => ({ value, label }))} allLabel="Todas" />
              <FilterSelect label="Local" value={f.location_kind} onChange={(v) => table.setFilter('location_kind', v)} options={Object.entries(LOCATION_KIND_LABEL).map(([value, label]) => ({ value, label }))} />
            </>
          }
        />
      </Card>
    </div>
  )
}
