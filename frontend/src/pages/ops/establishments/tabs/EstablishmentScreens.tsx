/** Aba "Telas" do estabelecimento — GET /screens?establishment_id= + presença em lote. */
import * as React from 'react'
import { Link } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Monitor, Plus } from 'lucide-react'
import { Card, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Can } from '@/components/auth/guards'
import { DataTable } from '@/components/patterns/data-table'
import { useTableState } from '@/hooks/useTableState'
import { qk } from '@/lib/queryKeys'
import { screensApi } from '@/pages/ops/api/client'
import { useScreensPresence } from '@/pages/ops/api/hooks'
import type { Establishment } from '@/pages/ops/api/types'
import { screenColumns } from '../../screens/screenColumns'

export function EstablishmentScreens({ est }: { est: Establishment }) {
  const table = useTableState({ defaultPageSize: 25, prefix: 'tl_' })
  const params = { establishment_id: est.id, page: table.state.page, page_size: table.state.pageSize }
  const q = useQuery({
    queryKey: qk.list('screens', params),
    queryFn: () => screensApi.list(params),
    placeholderData: keepPreviousData,
  })
  const ids = React.useMemo(() => (q.data?.items ?? []).filter((s) => s.status !== 'DECOMMISSIONED').map((s) => s.id), [q.data])
  const presence = useScreensPresence(ids)
  return (
    <Card>
      <CardHeader>
        <CardTitle>Telas vinculadas</CardTitle>
        <Can permission="screens.manage">
          {est.is_active ? (
            <Button asChild variant="secondary">
              <Link to={`/telas/nova?establishment_id=${est.id}`}>
                <Plus aria-hidden="true" />
                Adicionar tela
              </Link>
            </Button>
          ) : null}
        </Can>
      </CardHeader>
      <DataTable
        caption={`Telas de ${est.name}`}
        columns={screenColumns({
          presence: presence.data,
          presenceLoading: presence.isLoading,
          basePath: '/telas',
          showEstablishment: false,
          showInventory: true,
        })}
        data={q.data}
        getRowId={(s) => s.id}
        controller={table}
        isLoading={q.isLoading}
        isFetching={q.isFetching}
        error={q.error}
        onRetry={() => void q.refetch()}
        empty={{
          icon: Monitor,
          title: 'Nenhuma tela neste estabelecimento.',
          description: est.is_active ? 'Cadastre a tela e depois pareie o player instalado na TV.' : 'Estabelecimento inativo.',
        }}
      />
    </Card>
  )
}
