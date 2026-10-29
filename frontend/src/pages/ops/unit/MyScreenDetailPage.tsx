/**
 * Minha unidade → detalhe da tela (dono do estabelecimento): status de comunicação, último contato,
 * programação aplicada e PROGRAMAÇÃO prevista mostrando apenas o código RD do anúncio e o tipo
 * (nunca anunciante, campanha ou valores). Fonte segura: GET /scheduler/screens/{id}/programming
 * (W-INV). A rota de grade RD (`/grid`) NÃO é usada aqui por expor dados comerciais.
 */
import * as React from 'react'
import { Link, useParams } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { CalendarClock, ListOrdered } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { EmptyState, ErrorState, NoPermission, PageSkeleton } from '@/components/patterns/states'
import { DetailList } from '@/components/ops/detail-list'
import { PresenceBadge } from '@/components/ops/presence-badge'
import { lastContactOf } from '@/components/ops/presence-utils'
import { RealtimeStale } from '@/components/ops/realtime-stale'
import { RemovalRequestButton } from '@/components/ops/removal-request'
import { Can } from '@/components/auth/guards'
import { isApiError } from '@/lib/errors'
import { qk } from '@/lib/queryKeys'
import { formatDateTime, formatDuration, formatTime } from '@/lib/format'
import { presenceToSummary, scheduleApi, screensApi } from '@/pages/ops/api/client'
import { useScreenPresence } from '@/pages/ops/api/hooks'
import { CREATIVE_KIND_LABEL, ORIENTATION_LABEL, SCREEN_STATUS_LABEL, labelOf } from '@/pages/ops/api/labels'
import { sanitizeProgramming } from './programming'
import { externalBlockStart } from '../screens/scheduleUtils'

export default function MyScreenDetailPage() {
  const { id = '' } = useParams()
  const [hours, setHours] = React.useState('6')
  const q = useQuery({ queryKey: qk.detail('screens', id), queryFn: () => screensApi.get(id) })
  const presence = useScreenPresence(q.data && q.data.status !== 'DECOMMISSIONED' ? id : undefined)
  const prog = useQuery({
    queryKey: qk.sub('schedule', 'programming', id, hours),
    queryFn: () => scheduleApi.programming(id, Number(hours)).then(sanitizeProgramming),
    enabled: !!q.data,
    placeholderData: keepPreviousData,
    retry: false,
  })
  const s = q.data
  const crumbs = React.useMemo(() => [{ label: 'Minhas telas', to: '/unidade/telas' }, { label: s?.friendly_id ?? 'Tela' }], [s?.friendly_id])

  if (q.isLoading) return <PageSkeleton />
  if (isApiError(q.error) && q.error.isForbidden) return <NoPermission permission="screens.view" />
  if (q.error || !s)
    return (
      <ErrorState
        error={q.error}
        title={isApiError(q.error) && q.error.isNotFound ? 'Tela não encontrada ou fora do seu acesso' : undefined}
        onRetry={() => void q.refetch()}
      />
    )

  const tz = s.establishment?.timezone
  const summary = presence.data ? presenceToSummary(presence.data) : undefined
  const last = lastContactOf(summary)
  const progForbidden = isApiError(prog.error) && prog.error.isForbidden
  const rot = prog.data?.rotation
  const internal = (prog.data?.internal_pattern ?? []).filter((blk) => blk.length)

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title={`${s.friendly_id} · ${s.name}`}
        description={s.establishment?.name}
        breadcrumbs={crumbs}
        actions={<RemovalRequestButton screenId={s.id} />}
      />
      <RealtimeStale updatedAt={presence.dataUpdatedAt || q.dataUpdatedAt} onRefresh={() => { void q.refetch(); void presence.refetch() }} />
      <div className="grid grid-cols-1 gap-6 xl:grid-cols-12">
        <Card className="xl:col-span-5">
          <CardHeader>
            <CardTitle>Status da tela</CardTitle>
            {s.status !== 'DECOMMISSIONED' ? <PresenceBadge presence={summary} loading={presence.isLoading} timeZone={tz} /> : null}
          </CardHeader>
          <DetailList
            columns={1}
            items={[
              { label: 'Cadastro', value: labelOf(SCREEN_STATUS_LABEL, s.status) },
              { label: 'Último contato do player', value: last ? formatDateTime(last, tz) : null },
              { label: 'Programação aplicada', value: summary?.manifest_version_applied != null ? `Versão ${summary.manifest_version_applied}` : null },
              {
                label: 'Atualização em andamento',
                value: summary?.manifest_version_pending != null ? `Baixando a versão ${summary.manifest_version_pending}` : 'Nenhuma',
              },
              { label: 'Orientação', value: labelOf(ORIENTATION_LABEL, s.orientation) },
            ]}
          />
          <CardDescription>
            “Online” indica que o player está se comunicando. Problemas? Abra um chamado no Suporte informando o código da tela.
          </CardDescription>
        </Card>

        <Card className="xl:col-span-7">
          <CardHeader>
            <div className="flex flex-col gap-1">
              <CardTitle>Programação prevista</CardTitle>
              <CardDescription>Somente o código do anúncio e o tipo de conteúdo, no fuso {tz ?? 'do estabelecimento'}.</CardDescription>
            </div>
            <ToggleGroup type="single" value={hours} onValueChange={(v) => v && setHours(v)} aria-label="Horizonte da programação">
              {['3', '6', '12'].map((h) => (
                <ToggleGroupItem key={h} value={h}>
                  {h} h
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
          </CardHeader>
          {prog.isLoading ? (
            <Skeleton className="h-48 w-full" />
          ) : progForbidden ? (
            <NoPermission permission="screens.view" title="Programação indisponível para o seu perfil" />
          ) : prog.error ? (
            <ErrorState error={prog.error} onRetry={() => void prog.refetch()} size="compact" />
          ) : !prog.data?.blocks.length ? (
            <EmptyState icon={CalendarClock} size="compact" headingLevel="h3" title="Nada programado no período." />
          ) : (
            <div className="flex flex-col gap-4">
              {rot ? (
                <p className="m-0 text-sm text-text-muted">
                  Ciclo de {formatDuration(rot.cycle_ms / 1000)}: {formatDuration(rot.internal_ms / 1000)} do seu conteúdo interno +{' '}
                  {formatDuration(rot.external_ms / 1000)} de anúncios.
                </p>
              ) : null}
              <ol className="m-0 flex max-h-[420px] list-none flex-col divide-y divide-border overflow-y-auto p-0" aria-label="Blocos de anúncios previstos">
                {prog.data.blocks.map((b) => {
                  const start = rot ? externalBlockStart(b.bucket_ms, prog.data!.anchor_ms, rot.cycle_ms, rot.internal_ms) : null
                  return (
                    <li key={b.bucket_ms} className="flex flex-col gap-1 py-2">
                      <span className="text-sm font-semibold text-text">
                        {start !== null ? `Anúncios às ${formatTime(start, tz)}` : `Ciclo das ${formatTime(b.bucket_ms, tz)}`}
                      </span>
                      {b.entries.length ? (
                        <ul className="m-0 flex list-none flex-wrap gap-2 p-0">
                          {b.entries.map((e, i) => (
                            <li key={`${b.bucket_ms}-${i}`}>
                              <Badge tone={e.kind === 'EXTERNAL' ? 'info' : 'neutral'}>
                                {e.code} · {labelOf(CREATIVE_KIND_LABEL, e.kind)}
                              </Badge>
                            </li>
                          ))}
                        </ul>
                      ) : (
                        <span className="text-xs text-text-muted">Sem anúncio previsto — a tela exibe o conteúdo de contingência.</span>
                      )}
                    </li>
                  )
                })}
              </ol>
              <div className="flex flex-col gap-2">
                <h3 className="m-0 text-title-card text-text">Seu conteúdo interno nos blocos reservados</h3>
                {internal.length ? (
                  <ol className="m-0 flex list-none flex-col gap-2 p-0" aria-label="Sequência do conteúdo interno por bloco">
                    {internal.map((blk, i) => (
                      <li key={i} className="flex flex-wrap items-center gap-2 text-sm">
                        <span className="text-xs text-text-muted">Bloco {i + 1}:</span>
                        {blk.map((x, j) => (
                          <Badge key={`${i}-${j}`} tone="neutral">
                            {x.name ?? x.code} · {formatDuration(x.duration_ms / 1000)}
                          </Badge>
                        ))}
                      </li>
                    ))}
                  </ol>
                ) : (
                  <p className="m-0 text-sm text-text-muted">Sem conteúdo interno: os blocos reservados exibem o conteúdo de contingência.</p>
                )}
              </div>
            </div>
          )}
          <Can permission="playlists.reorder">
            <Button asChild variant="secondary" className="w-fit">
              <Link to="/unidade/programacao">
                <ListOrdered aria-hidden="true" />
                Ordenar meu conteúdo interno
              </Link>
            </Button>
          </Can>
        </Card>
      </div>
    </div>
  )
}
