/** Histórico da tela (append-only): cadastro, status, troca de estabelecimento/TV/TV Box, retirada. */
import * as React from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { History } from 'lucide-react'
import { Card, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState, ErrorState } from '@/components/patterns/states'
import { EstablishmentName, UserName } from '@/components/ops/entity-name'
import { qk } from '@/lib/queryKeys'
import { formatDateTime } from '@/lib/format'
import { screensApi } from '@/pages/ops/api/client'
import { SCREEN_HISTORY_LABEL, SCREEN_STATUS_LABEL, labelOf } from '@/pages/ops/api/labels'
import type { Screen } from '@/pages/ops/api/types'

function summarize(obj: Record<string, unknown> | null): string | null {
  if (!obj) return null
  const parts = Object.entries(obj)
    .filter(([k]) => !['establishment_id', 'friendly_id'].includes(k))
    .slice(0, 6)
    .map(([k, v]) => `${k}: ${k === 'status' ? labelOf(SCREEN_STATUS_LABEL, String(v)) : v === null ? '—' : String(v)}`)
  return parts.length ? parts.join(' · ') : null
}

export function ScreenHistoryTab({ screen }: { screen: Screen }) {
  const tz = screen.establishment?.timezone
  const [page, setPage] = React.useState(1)
  const q = useQuery({
    queryKey: qk.sub('screens', 'history', screen.id, page),
    queryFn: () => screensApi.history(screen.id, page),
    placeholderData: keepPreviousData,
  })
  const pages = Math.max(1, Math.ceil((q.data?.total ?? 0) / 25))
  return (
    <Card>
      <CardHeader>
        <CardTitle>Histórico da tela</CardTitle>
      </CardHeader>
      {q.isLoading ? (
        <Skeleton className="h-40 w-full" />
      ) : q.error && !q.data ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} size="compact" />
      ) : !q.data?.items.length ? (
        <EmptyState icon={History} size="compact" headingLevel="h3" title="Sem eventos no histórico." />
      ) : (
        <>
          <ol className="m-0 flex list-none flex-col divide-y divide-border p-0">
            {q.data.items.map((h) => (
              <li key={h.id} className="flex flex-col gap-1 py-3">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className="font-semibold text-text">{labelOf(SCREEN_HISTORY_LABEL, h.event)}</span>
                  <time className="text-xs text-text-muted" dateTime={h.created_at ?? undefined}>
                    {formatDateTime(h.created_at, tz)}
                  </time>
                </div>
                {h.event === 'MOVED' ? (
                  <span className="text-sm text-text">
                    De <EstablishmentName id={h.from_establishment_id} /> para <EstablishmentName id={h.establishment_id} />
                  </span>
                ) : null}
                {summarize(h.after) ? <span className="text-xs text-text-muted">{summarize(h.after)}</span> : null}
                {h.reason ? <span className="text-sm text-text">Motivo: {h.reason}</span> : null}
                <span className="text-xs text-text-muted">
                  Por <UserName id={h.actor_user_id} />
                </span>
              </li>
            ))}
          </ol>
          {pages > 1 ? (
            <nav aria-label="Paginação do histórico da tela" className="flex items-center justify-end gap-2 text-sm text-text-muted">
              <Button variant="secondary" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
                Anterior
              </Button>
              <span aria-current="page">
                Página {page} de {pages}
              </span>
              <Button variant="secondary" disabled={page >= pages} onClick={() => setPage((p) => p + 1)}>
                Próxima
              </Button>
            </nav>
          ) : null}
        </>
      )}
    </Card>
  )
}
