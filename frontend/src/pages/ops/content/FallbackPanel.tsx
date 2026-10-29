/**
 * Conteúdo de contingência (fallback): exibido quando não há conteúdo válido para o bloco
 * (ex.: tudo vencido/offline). Escopo GLOBAL (todas as telas) ou por estabelecimento.
 * GET/POST /internal-content/fallback, DELETE /internal-content/fallback/{id} (RD global).
 */
import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { LifeBuoy, Plus, Trash2 } from 'lucide-react'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { Field, FieldLabel } from '@/components/ui/field'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { ConfirmDialog } from '@/components/patterns/confirm-dialog'
import { EmptyState, ErrorState, NoPermission } from '@/components/patterns/states'
import { Can } from '@/components/auth/guards'
import { CreativeLabel, EstablishmentName } from '@/components/ops/entity-name'
import { OptionSelect } from '@/components/ops/filter-select'
import { CreativePicker, EstablishmentPicker } from '@/components/ops/pickers'
import { errorMessage, isApiError } from '@/lib/errors'
import { qk } from '@/lib/queryKeys'
import { contentApi } from '@/pages/ops/api/client'
import { FALLBACK_PURPOSE_LABEL, labelOf } from '@/pages/ops/api/labels'
import type { FallbackItem } from '@/pages/ops/api/types'

export function FallbackPanel({ establishmentId }: { establishmentId: string | null }) {
  const qc = useQueryClient()
  const list = useQuery({ queryKey: qk.sub('playlists', 'fallback'), queryFn: () => contentApi.fallback() })
  const [open, setOpen] = React.useState(false)
  const [scope, setScope] = React.useState<'GLOBAL' | 'ESTABLISHMENT'>('GLOBAL')
  const [estId, setEstId] = React.useState<string | null>(establishmentId)
  const [creativeId, setCreativeId] = React.useState<string | null>(null)
  const [purpose, setPurpose] = React.useState<string | null>('RD_DEFAULT')
  const [position, setPosition] = React.useState('0')
  const [removing, setRemoving] = React.useState<FallbackItem | null>(null)

  const add = useMutation({
    mutationFn: () =>
      contentApi.addFallback({
        scope,
        establishment_id: scope === 'ESTABLISHMENT' ? estId : null,
        creative_id: creativeId,
        purpose,
        position: Number(position) || 0,
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['playlists'] })
      setOpen(false)
      setCreativeId(null)
      toast.success('Conteúdo de contingência adicionado.')
    },
  })
  const valid = !!creativeId && !!purpose && (scope === 'GLOBAL' || !!estId) && /^\d{1,6}$/.test(position)

  if (isApiError(list.error) && list.error.isForbidden)
    return <NoPermission permission="playlists.view" title="Contingência é gerida pela equipe RD com acesso global" />

  const items = list.data?.items ?? []
  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader>
          <div className="flex flex-col gap-1">
            <CardTitle>Conteúdo de contingência</CardTitle>
            <CardDescription>Exibido quando não há conteúdo válido para um bloco. Prioridade mais baixa da grade.</CardDescription>
          </div>
          <Can permission="playlists.manage">
            <Button variant="secondary" onClick={() => setOpen((o) => !o)}>
              <Plus aria-hidden="true" />
              Adicionar
            </Button>
          </Can>
        </CardHeader>
        {open ? (
          <div className="flex flex-col gap-4 rounded-lg border border-border p-4">
            <div className="flex flex-col gap-2">
              <span id="fb-scope" className="text-sm font-semibold text-text">
                Escopo
              </span>
              <ToggleGroup type="single" value={scope} onValueChange={(v) => v && setScope(v as typeof scope)} aria-labelledby="fb-scope">
                <ToggleGroupItem value="GLOBAL">Todas as telas</ToggleGroupItem>
                <ToggleGroupItem value="ESTABLISHMENT">Um estabelecimento</ToggleGroupItem>
              </ToggleGroup>
            </div>
            {scope === 'ESTABLISHMENT' ? <EstablishmentPicker value={estId} onChange={setEstId} required /> : null}
            <CreativePicker
              kinds={['FALLBACK', 'INSTITUTIONAL']}
              value={creativeId}
              onChange={setCreativeId}
              required
              legend="Peça (contingência ou institucional, aprovada)"
              description="Peças novas são cadastradas e aprovadas na aba “Peças e revisão”."
            />
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <Field required>
                <FieldLabel>Finalidade</FieldLabel>
                <OptionSelect
                  value={purpose}
                  onChange={setPurpose}
                  options={Object.entries(FALLBACK_PURPOSE_LABEL).map(([value, label]) => ({ value, label }))}
                />
              </Field>
              <Field>
                <FieldLabel>Posição</FieldLabel>
                <Input value={position} onChange={(e) => setPosition(e.target.value.replace(/\D/g, ''))} inputMode="numeric" />
              </Field>
            </div>
            {add.error ? (
              <Alert variant="danger">
                <AlertDescription className="text-text">{errorMessage(add.error)}</AlertDescription>
              </Alert>
            ) : null}
            <div className="flex flex-wrap gap-2">
              <Button onClick={() => add.mutate()} disabled={!valid} loading={add.isPending}>
                Salvar contingência
              </Button>
              <Button variant="secondary" onClick={() => setOpen(false)} disabled={add.isPending}>
                Cancelar
              </Button>
            </div>
          </div>
        ) : null}
        {list.isLoading ? (
          <Skeleton className="h-32 w-full" />
        ) : list.error ? (
          <ErrorState error={list.error} onRetry={() => void list.refetch()} size="compact" />
        ) : !items.length ? (
          <EmptyState
            icon={LifeBuoy}
            size="compact"
            headingLevel="h3"
            title="Nenhum conteúdo de contingência."
            description="Sem contingência, blocos sem conteúdo válido ficam com a tela padrão do player."
          />
        ) : (
          <ul className="m-0 flex list-none flex-col divide-y divide-border p-0">
            {items.map((f) => (
              <li key={f.id} className="flex flex-wrap items-center justify-between gap-2 py-3">
                <div className="flex flex-col gap-0.5">
                  <span className="font-semibold text-text">{labelOf(FALLBACK_PURPOSE_LABEL, f.purpose)}</span>
                  <span className="text-sm text-text">
                    <CreativeLabel id={f.creative_id} />
                  </span>
                  <span className="text-xs text-text-muted">
                    {f.scope === 'GLOBAL' ? 'Todas as telas' : <EstablishmentName id={f.establishment_id} />} · posição {f.position}
                  </span>
                </div>
                <div className="flex items-center gap-2">
                  <Badge tone={f.active ? 'success' : 'neutral'}>{f.active ? 'Ativo' : 'Inativo'}</Badge>
                  <Can permission="playlists.manage">
                    <Button variant="ghost" size="icon" aria-label={`Remover contingência ${labelOf(FALLBACK_PURPOSE_LABEL, f.purpose)}`} onClick={() => setRemoving(f)}>
                      <Trash2 aria-hidden="true" />
                    </Button>
                  </Can>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>
      <ConfirmDialog
        open={!!removing}
        onOpenChange={(o) => !o && setRemoving(null)}
        title="Remover este conteúdo de contingência?"
        consequences={['As telas do escopo deixam de usar esta peça como contingência na próxima sincronização.']}
        confirmLabel="Remover"
        destructive
        onConfirm={async () => {
          await contentApi.removeFallback(removing!.id)
          void qc.invalidateQueries({ queryKey: ['playlists'] })
          toast.success('Contingência removida.')
        }}
      />
    </div>
  )
}
