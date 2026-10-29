/**
 * Minha unidade → Programação: o estabelecimento ORDENA o conteúdo interno dos seus blocos
 * reservados (playlists.reorder). Não envia, cria nem remove conteúdo (só a RD).
 * Backend: GET /internal-content?establishment_id=, PUT /internal-content/order (W-INV).
 */
import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Store } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Field, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { EmptyState, ErrorState, NoPermission } from '@/components/patterns/states'
import { InternalOrderList } from '@/components/ops/internal-order-list'
import { OptionSelect } from '@/components/ops/filter-select'
import { RealtimeStale } from '@/components/ops/realtime-stale'
import { useCan } from '@/hooks/usePermissions'
import { errorMessage, isApiError } from '@/lib/errors'
import { qk } from '@/lib/queryKeys'
import { contentApi, establishmentsApi } from '@/pages/ops/api/client'

export default function UnitSchedulePage() {
  const can = useCan()
  const qc = useQueryClient()
  const ests = useQuery({
    queryKey: qk.list('establishments', { ctx: 'unit-schedule', page_size: 100 }),
    queryFn: () => establishmentsApi.list({ page_size: 100, is_active: true }),
  })
  const [estId, setEstId] = React.useState<string | null>(null)
  React.useEffect(() => {
    if (!estId && ests.data?.items.length) setEstId(ests.data.items[0].id)
  }, [ests.data, estId])

  const list = useQuery({
    queryKey: qk.sub('playlists', 'internal', estId ?? '', 'common'),
    queryFn: () => contentApi.internal(estId!),
    enabled: !!estId,
  })
  const reorder = useMutation({
    mutationFn: (ids: string[]) => contentApi.reorder({ establishment_id: estId!, item_ids: ids }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['playlists'] })
      toast.success('Ordem salva. As telas recebem a nova sequência na próxima sincronização.')
    },
    onError: (e) => toast.error(errorMessage(e)),
  })

  if (isApiError(ests.error) && ests.error.isForbidden) return <NoPermission permission="establishments.view" />

  return (
    <div className="flex flex-col gap-6">
      <PageHeader title="Programação" description="Ordem do seu conteúdo interno nos blocos reservados ao estabelecimento." />
      <RealtimeStale updatedAt={list.dataUpdatedAt} onRefresh={() => void list.refetch()} />
      {ests.isLoading ? (
        <Skeleton className="h-24 w-full" />
      ) : ests.error ? (
        <ErrorState error={ests.error} onRetry={() => void ests.refetch()} />
      ) : !ests.data?.items.length ? (
        <EmptyState icon={Store} title="Nenhum estabelecimento ativo no seu acesso." />
      ) : (
        <>
          {ests.data.items.length > 1 ? (
            <Card>
              <Field className="max-w-md">
                <FieldLabel>Estabelecimento</FieldLabel>
                <OptionSelect
                  value={estId}
                  onChange={setEstId}
                  options={ests.data.items.map((e) => ({ value: e.id, label: `${e.name} — ${e.city}/${e.state}` }))}
                />
              </Field>
            </Card>
          ) : null}
          <Card>
            <CardHeader>
              <div className="flex flex-col gap-1">
                <CardTitle>Conteúdo interno</CardTitle>
                <CardDescription>
                  O envio e a troca das peças são feitos pela equipe RD (peça pelo Suporte). Aqui você define a ordem de exibição.
                </CardDescription>
              </div>
            </CardHeader>
            {!can('playlists.reorder') ? (
              <Alert variant="info">
                <AlertDescription className="text-text">Seu perfil só visualiza a ordem; a reordenação não está liberada.</AlertDescription>
              </Alert>
            ) : null}
            {list.isLoading ? (
              <Skeleton className="h-40 w-full" />
            ) : list.error ? (
              <ErrorState error={list.error} onRetry={() => void list.refetch()} size="compact" />
            ) : (
              <InternalOrderList
                items={list.data?.items ?? []}
                canReorder={can('playlists.reorder')}
                canManage={false}
                saving={reorder.isPending}
                onSaveOrder={(ids) => reorder.mutateAsync(ids)}
                emptyText="Ainda não há conteúdo interno na sua lista."
              />
            )}
          </Card>
        </>
      )}
    </div>
  )
}
