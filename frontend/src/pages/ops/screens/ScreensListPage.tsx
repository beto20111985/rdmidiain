/**
 * Telas (RD): código TEL-UF-XXXX, estabelecimento, status de cadastro, presença em tempo real
 * (W-PLAY; evento screen.status atualiza a linha), versão do player, manifesto aplicado e % download.
 * Backend: GET /screens (W-ORG) + presença em lote (W-PLAY) — sem polling.
 */
import * as React from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Monitor, Plus, QrCode } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Can } from '@/components/auth/guards'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Field, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { DataTable } from '@/components/patterns/data-table'
import { NoPermission } from '@/components/patterns/states'
import { FilterSelect } from '@/components/ops/filter-select'
import { RealtimeStale } from '@/components/ops/realtime-stale'
import { EstablishmentName } from '@/components/ops/entity-name'
import { useTableState } from '@/hooks/useTableState'
import { isApiError } from '@/lib/errors'
import { qk } from '@/lib/queryKeys'
import { formatNumber } from '@/lib/format'
import { screensApi } from '@/pages/ops/api/client'
import { useScreensPresence } from '@/pages/ops/api/hooks'
import { SCREEN_STATUS_LABEL, UFS } from '@/pages/ops/api/labels'
import { screenColumns } from './screenColumns'

const FILTERS = ['status', 'state', 'city', 'screen_class', 'sellable', 'establishment_id']

export default function ScreensListPage() {
  const table = useTableState({ defaultPageSize: 25, filterKeys: FILTERS })
  const f = table.state.filters
  const [, setSp] = useSearchParams()
  const [cityDraft, setCityDraft] = React.useState(f.city ?? '')
  React.useEffect(() => setCityDraft(f.city ?? ''), [f.city])

  const apiParams = React.useMemo(() => {
    const { q, city, sort: _sort, ...rest } = table.apiParams as Record<string, string | number | undefined>
    return { ...rest, search: q || undefined, city_key: city && f.state ? `${f.state}:${city}` : undefined }
  }, [table.apiParams, f.state])

  const q = useQuery({
    queryKey: qk.list('screens', apiParams),
    queryFn: () => screensApi.list(apiParams),
    placeholderData: keepPreviousData,
  })
  const ids = React.useMemo(() => (q.data?.items ?? []).filter((s) => s.status !== 'DECOMMISSIONED').map((s) => s.id), [q.data])
  const presence = useScreensPresence(ids)

  if (isApiError(q.error) && q.error.isForbidden) return <NoPermission permission="screens.view" />

  const columns = screenColumns({
    presence: presence.data,
    presenceLoading: presence.isLoading,
    basePath: '/telas',
    showEstablishment: true,
    establishmentLink: true,
    showInventory: true,
  })
  const filtersActive = !!table.state.q || Object.keys(f).length > 0

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Telas"
        description={q.data ? `${formatNumber(q.data.total)} tela(s) no seu acesso` : undefined}
        actions={
          <div className="flex flex-wrap gap-2">
            <Can permission="screens.pair">
              <Button asChild variant="secondary">
                <Link to="/telas/parear">
                  <QrCode aria-hidden="true" />
                  Parear tela
                </Link>
              </Button>
            </Can>
            <Can permission="screens.manage">
              <Button asChild>
                <Link to={f.establishment_id ? `/telas/nova?establishment_id=${f.establishment_id}` : '/telas/nova'}>
                  <Plus aria-hidden="true" />
                  Adicionar tela
                </Link>
              </Button>
            </Can>
          </div>
        }
      />
      <RealtimeStale updatedAt={q.dataUpdatedAt} onRefresh={() => { void q.refetch(); void presence.refetch() }} refreshing={q.isFetching} />
      {f.establishment_id ? (
        <Alert variant="info">
          <AlertDescription className="flex flex-wrap items-center gap-2 text-text">
            Mostrando as telas de <EstablishmentName id={f.establishment_id} />.
            <Button
              variant="tertiary"
              onClick={() =>
                setSp((p) => {
                  const n = new URLSearchParams(p)
                  n.delete('establishment_id')
                  return n
                }, { replace: true })
              }
            >
              Ver todas
            </Button>
          </AlertDescription>
        </Alert>
      ) : null}
      {presence.error ? (
        <Alert variant="warning">
          <AlertDescription className="text-text">
            Não foi possível obter a presença das telas agora. O cadastro está atualizado; tente “Atualizar agora”.
          </AlertDescription>
        </Alert>
      ) : null}
      <Card>
        <DataTable
          caption="Telas"
          columns={columns}
          data={q.data}
          getRowId={(s) => s.id}
          controller={table}
          isLoading={q.isLoading}
          isFetching={q.isFetching}
          error={q.error}
          onRetry={() => void q.refetch()}
          filtersActive={filtersActive}
          empty={{
            icon: Monitor,
            title: 'Adicione sua primeira tela para começar.',
            description: 'Cada tela recebe um código TEL-UF-XXXX e depois é pareada com o player instalado na TV.',
            action: (
              <Can permission="screens.manage">
                <Button asChild>
                  <Link to="/telas/nova">Adicionar tela</Link>
                </Button>
              </Can>
            ),
          }}
          toolbar={
            <>
              <Field className="w-full sm:max-w-xs">
                <FieldLabel>Buscar</FieldLabel>
                <Input value={table.state.q} onChange={(e) => table.setQuery(e.target.value)} placeholder="Código TEL-… ou nome" />
              </Field>
              <FilterSelect
                label="Cadastro"
                value={f.status}
                onChange={(v) => table.setFilter('status', v)}
                options={Object.entries(SCREEN_STATUS_LABEL).map(([value, label]) => ({ value, label }))}
              />
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
                label="Classe"
                value={f.screen_class}
                onChange={(v) => table.setFilter('screen_class', v)}
                options={[
                  { value: 'STANDARD', label: 'Padrão' },
                  { value: 'PREMIUM', label: 'Premium' },
                ]}
                allLabel="Todas"
              />
              <FilterSelect
                label="Vendável"
                value={f.sellable}
                onChange={(v) => table.setFilter('sellable', v)}
                options={[
                  { value: 'true', label: 'Vendável' },
                  { value: 'false', label: 'Fora do inventário' },
                ]}
              />
            </>
          }
        />
      </Card>
    </div>
  )
}
