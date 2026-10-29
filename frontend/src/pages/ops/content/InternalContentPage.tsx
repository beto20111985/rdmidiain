/**
 * Conteúdo interno e playlists (RD): ordem do conteúdo interno por estabelecimento (lista comum)
 * ou por tela (grade própria), ativar/desativar, remover, adicionar peças INTERNAS aprovadas; e
 * conteúdo de contingência (fallback) GLOBAL/por estabelecimento. Backend: /internal-content (W-INV).
 * Só a RD cria/remove (playlists.manage global); reordenar exige playlists.reorder.
 */
import * as React from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Plus, Siren } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Can } from '@/components/auth/guards'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Skeleton } from '@/components/ui/skeleton'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { ConfirmDialog } from '@/components/patterns/confirm-dialog'
import { EmptyState, ErrorState } from '@/components/patterns/states'
import { InternalOrderList } from '@/components/ops/internal-order-list'
import { CreativePicker, EstablishmentPicker, ScreenPicker } from '@/components/ops/pickers'
import { RealtimeStale } from '@/components/ops/realtime-stale'
import { FallbackPanel } from './FallbackPanel'
import { RdPiecesPanel } from './RdPiecesPanel'
import { useCan } from '@/hooks/usePermissions'
import { qk } from '@/lib/queryKeys'
import { errorMessage } from '@/lib/errors'
import { contentApi } from '@/pages/ops/api/client'
import { useScreenLookup } from '@/pages/ops/api/hooks'
import type { InternalItem } from '@/pages/ops/api/types'

export default function InternalContentPage() {
  const can = useCan()
  const qc = useQueryClient()
  const [sp, setSp] = useSearchParams()
  const estId = sp.get('estabelecimento')
  const screenId = sp.get('tela')
  const aba = sp.get('aba')
  const tab = aba === 'fallback' ? 'fallback' : aba === 'pecas' && can('creatives.view') ? 'pecas' : 'interno'
  const [adding, setAdding] = React.useState<string | null>(null)
  const [removing, setRemoving] = React.useState<InternalItem | null>(null)
  const screen = useScreenLookup(screenId)

  const setParam = (k: string, v: string | null) =>
    setSp(
      (p) => {
        const n = new URLSearchParams(p)
        if (v) n.set(k, v)
        else n.delete(k)
        if (k === 'estabelecimento') n.delete('tela')
        return n
      },
      { replace: true },
    )

  const key = qk.sub('playlists', 'internal', estId ?? '', screenId ?? 'common')
  const list = useQuery({
    queryKey: key,
    queryFn: () => contentApi.internal(estId!, screenId),
    enabled: !!estId,
  })
  const invalidate = () => void qc.invalidateQueries({ queryKey: ['playlists'] })
  const reorder = useMutation({
    mutationFn: (ids: string[]) => contentApi.reorder({ establishment_id: estId!, screen_id: screenId, item_ids: ids }),
    onSuccess: () => {
      invalidate()
      toast.success('Ordem salva. As telas recebem a nova sequência na próxima sincronização.')
    },
    onError: (e) => toast.error(errorMessage(e)),
  })
  const toggle = useMutation({
    mutationFn: ({ id, active }: { id: string; active: boolean }) => contentApi.patchInternal(id, active),
    onSuccess: (_d, v) => {
      invalidate()
      toast.success(v.active ? 'Peça ativada.' : 'Peça desativada.')
    },
    onError: (e) => toast.error(errorMessage(e)),
  })
  const add = useMutation({
    mutationFn: (creativeId: string) => contentApi.addInternal({ establishment_id: estId, screen_id: screenId, creative_id: creativeId }),
    onSuccess: () => {
      invalidate()
      setAdding(null)
      toast.success('Peça adicionada ao fim da lista.')
    },
  })

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Conteúdo interno e playlists"
        description="O conteúdo interno ocupa os blocos reservados ao estabelecimento em cada ciclo."
        actions={
          <Can permission="campaigns.kill">
            <Button asChild variant="destructive">
              <Link to="/conteudo-interno/emergencia">
                <Siren aria-hidden="true" />
                Comunicado de emergência
              </Link>
            </Button>
          </Can>
        }
      />
      <RealtimeStale updatedAt={list.dataUpdatedAt} onRefresh={() => void list.refetch()} refreshing={list.isFetching} />
      <Tabs value={tab} onValueChange={(v) => setParam('aba', v === 'interno' ? null : v)}>
        <TabsList aria-label="Tipo de conteúdo">
          <TabsTrigger value="interno">Conteúdo interno</TabsTrigger>
          <TabsTrigger value="fallback">Contingência (fallback)</TabsTrigger>
          {can('creatives.view') ? <TabsTrigger value="pecas">Peças e revisão</TabsTrigger> : null}
        </TabsList>
        <TabsContent value="interno" className="flex flex-col gap-6">
          <Card>
            <CardHeader>
              <CardTitle>Onde aplicar</CardTitle>
              <CardDescription>
                A lista comum vale para as telas com “grade comum do estabelecimento”; telas com grade própria têm lista separada.
              </CardDescription>
            </CardHeader>
            <EstablishmentPicker value={estId} onChange={(id) => setParam('estabelecimento', id)} required />
            {estId ? (
              <ScreenPicker
                value={screenId}
                onChange={(id) => setParam('tela', id)}
                establishmentId={estId}
                legend="Tela com grade própria (opcional)"
                description="Deixe em branco para editar a lista comum do estabelecimento."
              />
            ) : null}
            {screenId ? (
              <Button variant="tertiary" className="w-fit" onClick={() => setParam('tela', null)}>
                Voltar para a lista comum
              </Button>
            ) : null}
          </Card>

          {!estId ? (
            <EmptyState title="Escolha um estabelecimento" description="Selecione o estabelecimento para ver e ordenar o conteúdo interno." />
          ) : (
            <Card>
              <CardHeader>
                <CardTitle>{screenId ? `Lista própria da tela ${screen.data?.friendly_id ?? ''}` : 'Lista comum do estabelecimento'}</CardTitle>
                <Can permission="playlists.manage">
                  <Button variant="secondary" onClick={() => setAdding('')}>
                    <Plus aria-hidden="true" />
                    Adicionar peça
                  </Button>
                </Can>
              </CardHeader>
              {screenId && screen.data && screen.data.programming_mode !== 'CUSTOM' ? (
                <Alert variant="warning">
                  <AlertDescription className="text-text">
                    Esta tela usa a grade comum do estabelecimento; a lista própria só é usada se a tela estiver com “grade própria”.
                  </AlertDescription>
                </Alert>
              ) : null}
              {adding !== null ? (
                <div className="flex flex-col gap-3 rounded-lg border border-border p-4">
                  <CreativePicker
                    kinds={['INTERNAL']}
                    value={adding || null}
                    onChange={(id) => setAdding(id ?? '')}
                    required
                    legend="Peça de conteúdo interno (aprovada)"
                    description={
                      <>
                        Somente peças do tipo Conteúdo interno, aprovadas e com mídia pronta.
                        {can('creatives.view') ? (
                          <>
                            {' '}
                            Peças novas são cadastradas e revisadas em{' '}
                            <Link to="/conteudo-interno?aba=pecas" className="underline">
                              Peças e revisão
                            </Link>
                            .
                          </>
                        ) : null}
                      </>
                    }
                  />
                  {add.error ? (
                    <Alert variant="danger">
                      <AlertDescription className="text-text">{errorMessage(add.error)}</AlertDescription>
                    </Alert>
                  ) : null}
                  <div className="flex flex-wrap gap-2">
                    <Button onClick={() => adding && add.mutate(adding)} disabled={!adding} loading={add.isPending}>
                      Adicionar à lista
                    </Button>
                    <Button variant="secondary" onClick={() => setAdding(null)} disabled={add.isPending}>
                      Cancelar
                    </Button>
                  </div>
                </div>
              ) : null}
              {list.isLoading ? (
                <Skeleton className="h-40 w-full" />
              ) : list.error ? (
                <ErrorState error={list.error} onRetry={() => void list.refetch()} size="compact" />
              ) : (
                <InternalOrderList
                  items={list.data?.items ?? []}
                  canReorder={can('playlists.reorder')}
                  canManage={can('playlists.manage')}
                  saving={reorder.isPending}
                  onSaveOrder={(ids) => reorder.mutateAsync(ids)}
                  onToggle={(it, active) => toggle.mutate({ id: it.id, active })}
                  onRemove={(it) => setRemoving(it)}
                />
              )}
            </Card>
          )}
        </TabsContent>
        <TabsContent value="fallback">
          <FallbackPanel establishmentId={estId} />
        </TabsContent>
        {can('creatives.view') ? (
          <TabsContent value="pecas">
            <RdPiecesPanel />
          </TabsContent>
        ) : null}
      </Tabs>

      <ConfirmDialog
        open={!!removing}
        onOpenChange={(o) => !o && setRemoving(null)}
        title={`Remover ${removing?.name ?? 'a peça'} da lista?`}
        description="A peça continua na biblioteca; só sai desta lista de conteúdo interno."
        consequences={['As telas deixam de exibir esta peça na próxima sincronização.']}
        confirmLabel="Remover da lista"
        destructive
        onConfirm={async () => {
          await contentApi.removeInternal(removing!.id)
          invalidate()
          toast.success('Peça removida da lista.')
        }}
      />
    </div>
  )
}
