/**
 * Histórico (trilha de auditoria) de uma entidade — GET /audit?entity_type=&entity_id= (audit.view).
 * Sem a permissão, explica onde está o histórico em vez de mostrar dado parcial.
 */
import * as React from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { History } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState, ErrorState, NoPermission } from '@/components/patterns/states'
import { useCan } from '@/hooks/usePermissions'
import { qk } from '@/lib/queryKeys'
import { formatDateTime } from '@/lib/format'
import { auditApi } from '@/pages/ops/api/client'
import { UserName } from './entity-name'

function describeChange(after: Record<string, unknown> | null): string | null {
  const keys = Object.keys(after ?? {}).filter((k) => k !== 'updated_at')
  if (!keys.length) return null
  return keys.slice(0, 6).join(', ') + (keys.length > 6 ? '…' : '')
}

export function AuditHistory({ entityType, entityId, timeZone }: { entityType: string; entityId: string; timeZone?: string }) {
  const can = useCan()
  const [page, setPage] = React.useState(1)
  const enabled = can('audit.view')
  const q = useQuery({
    queryKey: qk.list('audit', { entity_type: entityType, entity_id: entityId, page }),
    queryFn: () => auditApi.list({ entity_type: entityType, entity_id: entityId, page, page_size: 20 }),
    enabled,
    placeholderData: keepPreviousData,
  })

  if (!enabled)
    return (
      <NoPermission
        permission="audit.view"
        title="Histórico disponível para quem acessa a auditoria"
        description="As alterações deste registro ficam na trilha de auditoria. Peça acesso a um administrador."
      />
    )
  if (q.isLoading)
    return (
      <div role="status" className="flex flex-col gap-2">
        <span className="sr-only">Carregando histórico…</span>
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-14 w-full" />
        ))}
      </div>
    )
  if (q.error && !q.data) return <ErrorState error={q.error} onRetry={() => void q.refetch()} size="compact" />
  const items = q.data?.items ?? []
  if (!items.length) return <EmptyState icon={History} size="compact" headingLevel="h3" title="Nenhuma alteração registrada." />
  const pages = Math.max(1, Math.ceil((q.data?.total ?? 0) / 20))
  return (
    <div className="flex flex-col gap-4">
      <ol className="m-0 flex list-none flex-col divide-y divide-border p-0">
        {items.map((a) => (
          <li key={a.id} className="flex flex-col gap-1 py-3">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <span className="font-semibold text-text">{a.action}</span>
              <time className="text-xs text-text-muted" dateTime={a.created_at}>
                {formatDateTime(a.created_at, timeZone)}
              </time>
            </div>
            <span className="text-xs text-text-muted">
              Por <UserName id={a.actor_user_id} />
              {a.real_user_id && a.real_user_id !== a.actor_user_id ? ' (impersonation)' : ''}
              {describeChange(a.after) ? ` · campos: ${describeChange(a.after)}` : ''}
            </span>
            {a.reason ? <span className="text-sm text-text">Motivo: {a.reason}</span> : null}
          </li>
        ))}
      </ol>
      {pages > 1 ? (
        <nav aria-label="Paginação do histórico" className="flex items-center justify-end gap-2 text-sm text-text-muted">
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
    </div>
  )
}
