/**
 * Minha unidade → Minhas telas (dono do estabelecimento): status de comunicação em tempo real,
 * último contato e programação aplicada. Sem dados comerciais de anunciantes. Pedido de retirada
 * de anúncio pelo código RD-XXXXXX (abre chamado no Suporte).
 */
import * as React from 'react'
import { Link } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { FileSignature, Monitor } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { DataTable } from '@/components/patterns/data-table'
import { NoPermission } from '@/components/patterns/states'
import { RealtimeStale } from '@/components/ops/realtime-stale'
import { RemovalRequestButton } from '@/components/ops/removal-request'
import { useTableState } from '@/hooks/useTableState'
import { isApiError } from '@/lib/errors'
import { qk } from '@/lib/queryKeys'
import { formatNumber } from '@/lib/format'
import { establishmentsApi, screensApi } from '@/pages/ops/api/client'
import { useScreensPresence } from '@/pages/ops/api/hooks'
import { screenColumns } from '../screens/screenColumns'

export default function MyScreensPage() {
  const table = useTableState({ defaultPageSize: 25 })
  const params = { page: table.state.page, page_size: table.state.pageSize }
  const q = useQuery({
    queryKey: qk.list('screens', { ...params, ctx: 'unit' }),
    queryFn: () => screensApi.list(params),
    placeholderData: keepPreviousData,
  })
  const pending = useQuery({
    queryKey: qk.list('establishments', { onboarding_status: 'AWAITING_CONTRACT', ctx: 'unit' }),
    queryFn: () => establishmentsApi.list({ onboarding_status: 'AWAITING_CONTRACT', page_size: 10 }),
  })
  const ids = React.useMemo(() => (q.data?.items ?? []).filter((s) => s.status !== 'DECOMMISSIONED').map((s) => s.id), [q.data])
  const presence = useScreensPresence(ids)
  const multi = new Set((q.data?.items ?? []).map((s) => s.establishment_id)).size > 1

  if (isApiError(q.error) && q.error.isForbidden) return <NoPermission permission="screens.view" />

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Minhas telas"
        description={q.data ? `${formatNumber(q.data.total)} tela(s)` : undefined}
        actions={<RemovalRequestButton />}
      />
      <RealtimeStale updatedAt={q.dataUpdatedAt} onRefresh={() => { void q.refetch(); void presence.refetch() }} refreshing={q.isFetching} />
      {pending.data?.items.length ? (
        <Alert variant="warning">
          <FileSignature aria-hidden="true" />
          <div className="flex flex-col gap-2">
            <AlertTitle>Contrato de parceria pendente</AlertTitle>
            <AlertDescription>
              {pending.data.items.map((e) => e.name).join(', ')}: aceite o contrato para que as telas passem a exibir anúncios.
            </AlertDescription>
            <Button asChild className="w-fit">
              <Link to="/unidade/contrato">Ler e aceitar o contrato</Link>
            </Button>
          </div>
        </Alert>
      ) : null}
      <Card>
        <DataTable
          caption="Minhas telas"
          columns={screenColumns({
            presence: presence.data,
            presenceLoading: presence.isLoading,
            basePath: '/unidade/telas',
            showEstablishment: multi,
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
            title: 'Nenhuma tela instalada ainda.',
            description: 'Assim que a equipe RD instalar e parear as TVs, elas aparecem aqui com o status em tempo real.',
          }}
        />
      </Card>
    </div>
  )
}
