/**
 * Programação da tela (W-INV): estado de inventário (política I/E, âncora do ciclo, vendável),
 * grade prevista das próximas horas (mesmo gerador do manifesto), versões de manifesto
 * (aplicada × gerada), regeneração e reset de âncora com justificativa (admin) + histórico da âncora.
 */
import * as React from 'react'
import { Link } from 'react-router-dom'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Anchor, CalendarClock, RefreshCw } from 'lucide-react'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { EmptyState, ErrorState, NoPermission } from '@/components/patterns/states'
import { DetailList } from '@/components/ops/detail-list'
import { ReasonDialog } from '@/components/ops/reason-dialog'
import { UserName } from '@/components/ops/entity-name'
import { useCan } from '@/hooks/usePermissions'
import { isApiError } from '@/lib/errors'
import { qk } from '@/lib/queryKeys'
import { formatDateTime, formatDuration, formatTime } from '@/lib/format'
import { scheduleApi } from '@/pages/ops/api/client'
import { CREATIVE_KIND_LABEL, labelOf } from '@/pages/ops/api/labels'
import type { ScreenGrid, Screen } from '@/pages/ops/api/types'
import { externalBlockStart } from '../scheduleUtils'

function GridView({ grid, tz }: { grid: ScreenGrid; tz: string }) {
  const blocks = grid.blocks
  if (!blocks.length) return <EmptyState icon={CalendarClock} size="compact" headingLevel="h3" title="Nenhum bloco no período." />
  return (
    <ol className="m-0 flex max-h-[480px] list-none flex-col divide-y divide-border overflow-y-auto p-0">
      {blocks.map((b) => {
        const s = externalBlockStart(b.bucket_ms, grid.anchor_ms, grid.rotation.cycle_ms, grid.rotation.internal_ms)
        return (
          <li key={b.bucket_ms} className="flex flex-col gap-1 py-2">
            <span className="text-sm font-semibold text-text">
              Bloco externo {s !== null ? `às ${formatTime(s, tz)}` : `do ciclo de ${formatTime(b.bucket_ms, tz)}`}
            </span>
            {b.entries.length ? (
              <ul className="m-0 flex list-none flex-wrap gap-2 p-0">
                {b.entries.map((e) => (
                  <li key={`${b.bucket_ms}-${e.u}`}>
                    <Badge tone={e.kind === 'EXTERNAL' ? 'info' : e.kind === 'INTERNAL' ? 'neutral' : 'outline'}>
                      +{formatDuration((e.u * 30_000) / 1000)} · {e.code} · {labelOf(CREATIVE_KIND_LABEL, e.kind)} · {Math.round(e.d / 1000)} s
                    </Badge>
                  </li>
                ))}
              </ul>
            ) : (
              <span className="text-xs text-text-muted">Sem entradas — o player exibe o conteúdo de contingência.</span>
            )}
          </li>
        )
      })}
    </ol>
  )
}

export function ScreenScheduleTab({ screen }: { screen: Screen }) {
  const can = useCan()
  const qc = useQueryClient()
  const tz = screen.establishment?.timezone ?? 'America/Sao_Paulo'
  const [hours, setHours] = React.useState('6')
  const [resetOpen, setResetOpen] = React.useState(false)
  const canInv = can('inventory.view')

  const state = useQuery({
    queryKey: qk.sub('inventory', 'screen-state', screen.id),
    queryFn: () => scheduleApi.state(screen.id),
    enabled: canInv,
  })
  const grid = useQuery({
    queryKey: qk.sub('schedule', 'grid', screen.id, hours),
    queryFn: () => scheduleApi.grid(screen.id, Number(hours)),
    enabled: canInv,
    placeholderData: keepPreviousData,
    retry: false,
  })
  const anchors = useQuery({
    queryKey: qk.sub('inventory', 'anchor-history', screen.id),
    queryFn: () => scheduleApi.anchorHistory(screen.id),
    enabled: canInv,
    retry: false,
  })
  const manifests = useQuery({
    queryKey: qk.sub('screens', 'manifests', screen.id),
    queryFn: () => scheduleApi.manifests(screen.id),
  })
  const regen = useMutation({
    mutationFn: () => scheduleApi.regenerate(screen.id),
    onSuccess: (r) => {
      void qc.invalidateQueries({ queryKey: ['screens'] })
      void qc.invalidateQueries({ queryKey: ['inventory'] })
      toast.success(r.version ? `Programação regenerada (versão ${r.version}).` : 'Programação já estava atualizada.')
    },
    onError: (e) => toast.error(isApiError(e) && e.isForbidden ? 'Somente a equipe RD com acesso global regenera a programação.' : String((e as Error).message)),
  })
  const reset = useMutation({
    mutationFn: (reason: string) => scheduleApi.resetAnchor(screen.id, reason),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['inventory'] })
      void qc.invalidateQueries({ queryKey: ['schedule'] })
      toast.success('Âncora do ciclo redefinida. A grade da tela será recalculada.')
    },
  })

  const st = state.data
  return (
    <div className="grid grid-cols-1 gap-6 xl:grid-cols-12">
      <Card className="xl:col-span-5">
        <CardHeader>
          <CardTitle>Ciclo e inventário</CardTitle>
          {can('screens.manage') ? (
            <Button variant="destructive" onClick={() => setResetOpen(true)} disabled={!st?.anchor_ms}>
              <Anchor aria-hidden="true" />
              Resetar âncora
            </Button>
          ) : null}
        </CardHeader>
        {!canInv ? (
          <NoPermission permission="inventory.view" title="Inventário indisponível para o seu perfil" />
        ) : state.isLoading ? (
          <Skeleton className="h-40 w-full" />
        ) : state.error ? (
          <ErrorState error={state.error} onRetry={() => void state.refetch()} size="compact" />
        ) : st ? (
          <DetailList
            columns={1}
            items={[
              {
                label: 'Ciclo vigente',
                value: `${st.policy.internal_seconds / 60} min interno + ${st.policy.external_seconds / 60} min externo (${st.policy.cycles_per_hour} ciclos/h)`,
              },
              { label: 'Unidades vendáveis por hora', value: `${st.policy.units_per_hour} × 30 s`, numeric: true },
              { label: 'Origem da política', value: st.policy.scope ?? null },
              { label: 'Âncora do ciclo', value: st.anchor_ms ? formatDateTime(st.anchor_ms, tz) : 'Ainda não definida (definida no 1º início do player)' },
              { label: 'Origem da âncora', value: st.anchor_source },
              { label: 'Disponível para venda', value: st.sellable ? 'Sim' : `Não${st.unsellable_reason ? ` — ${st.unsellable_reason}` : ''}` },
              { label: 'Versão de programação gerada', value: st.manifest_version ? `v${st.manifest_version}` : null },
              { label: 'Versão aplicada pelo player', value: st.manifest_applied_version ? `v${st.manifest_applied_version}` : null },
              { label: 'Itens não encaixados na última geração', value: st.manifest_unplaced, numeric: true },
            ]}
          />
        ) : null}
      </Card>

      <Card className="xl:col-span-7">
        <CardHeader>
          <div className="flex flex-col gap-1">
            <CardTitle>Grade prevista</CardTitle>
            <CardDescription>
              Prévia dos blocos externos (mesmo gerador do manifesto), no fuso {tz}; unidades não vendidas voltam ao conteúdo interno. Os
              blocos internos seguem a lista de{' '}
              <Link
                to={`/conteudo-interno?estabelecimento=${screen.establishment_id}${screen.programming_mode === 'CUSTOM' ? `&tela=${screen.id}` : ''}`}
                className="underline"
              >
                conteúdo interno
              </Link>
              . Agendamento não é prova de exibição.
            </CardDescription>
          </div>
          <ToggleGroup type="single" value={hours} onValueChange={(v) => v && setHours(v)} aria-label="Horizonte da prévia">
            {['3', '6', '12', '24'].map((h) => (
              <ToggleGroupItem key={h} value={h}>
                {h} h
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        </CardHeader>
        {!canInv ? (
          <NoPermission permission="inventory.view" />
        ) : grid.isLoading ? (
          <Skeleton className="h-64 w-full" />
        ) : isApiError(grid.error) && grid.error.isForbidden ? (
          <NoPermission permission="inventory.view" title="Prévia da grade restrita à equipe RD com acesso global" />
        ) : grid.error && !grid.data ? (
          <ErrorState error={grid.error} onRetry={() => void grid.refetch()} size="compact" />
        ) : grid.data ? (
          <GridView grid={grid.data} tz={tz} />
        ) : null}
      </Card>

      <Card className="xl:col-span-7">
        <CardHeader>
          <CardTitle>Versões da programação (manifesto)</CardTitle>
          {can('screens.manage') ? (
            <Button variant="secondary" onClick={() => regen.mutate()} loading={regen.isPending} disabled={screen.status === 'DECOMMISSIONED'}>
              <RefreshCw aria-hidden="true" />
              Regenerar agora
            </Button>
          ) : null}
        </CardHeader>
        {manifests.isLoading ? (
          <Skeleton className="h-32 w-full" />
        ) : manifests.error ? (
          <ErrorState error={manifests.error} onRetry={() => void manifests.refetch()} size="compact" />
        ) : !manifests.data?.items.length ? (
          <EmptyState size="compact" headingLevel="h3" title="Nenhuma versão gerada ainda." />
        ) : (
          <ol className="m-0 flex list-none flex-col divide-y divide-border p-0">
            {manifests.data.items.map((mv) => (
              <li key={mv.version} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
                <span className="font-semibold tabular-nums text-text">v{mv.version}</span>
                <span className="text-text-muted">
                  Gerada {formatDateTime(mv.generated_at, tz)} · {mv.reason ?? '—'}
                  {mv.unplaced_count ? ` · ${mv.unplaced_count} não encaixado(s)` : ''}
                </span>
                {mv.applied_at ? <Badge tone="success">Aplicada {formatDateTime(mv.applied_at, tz)}</Badge> : <Badge tone="warning">Não aplicada</Badge>}
              </li>
            ))}
          </ol>
        )}
      </Card>

      <Card className="xl:col-span-5">
        <CardHeader>
          <CardTitle>Histórico da âncora</CardTitle>
        </CardHeader>
        {!canInv ? null : anchors.isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : isApiError(anchors.error) && anchors.error.isForbidden ? (
          <p className="m-0 text-sm text-text-muted">Restrito à equipe RD com acesso global.</p>
        ) : anchors.error ? (
          <ErrorState error={anchors.error} onRetry={() => void anchors.refetch()} size="compact" />
        ) : !anchors.data?.items.length ? (
          <p className="m-0 text-sm text-text-muted">Sem alterações de âncora.</p>
        ) : (
          <ol className="m-0 flex list-none flex-col divide-y divide-border p-0">
            {anchors.data.items.map((a) => (
              <li key={a.id} className="flex flex-col gap-0.5 py-2 text-sm">
                <span className="text-text">
                  {a.new_anchor_ms ? formatDateTime(a.new_anchor_ms, tz) : '—'} <span className="text-xs text-text-muted">({a.source})</span>
                </span>
                <span className="text-xs text-text-muted">
                  {formatDateTime(a.created_at, tz)}
                  {a.actor_user_id ? (
                    <>
                      {' '}
                      · por <UserName id={a.actor_user_id} />
                    </>
                  ) : null}
                  {a.reason ? ` · ${a.reason}` : ''}
                </span>
              </li>
            ))}
          </ol>
        )}
      </Card>

      <ReasonDialog
        open={resetOpen}
        onOpenChange={setResetOpen}
        title={`Resetar a âncora do ciclo de ${screen.friendly_id}?`}
        description="A âncora define o início de cada ciclo interno/externo da tela. Resetar é excepcional e auditado."
        consequences={[
          'O ciclo da tela passa a começar no novo instante; a grade e o manifesto são recalculados.',
          'Exibições já registradas não são alteradas.',
        ]}
        confirmLabel="Resetar âncora"
        destructive
        requireText={screen.friendly_id}
        onConfirm={(reason) => reset.mutateAsync(reason)}
      />
    </div>
  )
}
