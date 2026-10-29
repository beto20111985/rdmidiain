/**
 * Fila de produção do designer — GET /design/queue (somente `design.work`): contagem por
 * etapa, pedidos sem responsável, "Assumir" e atribuição a outro designer.
 */
import * as React from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { ListOrdered, UserCheck } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { EmptyState, ErrorState, NoPermission, PageSkeleton } from '@/components/patterns/states'
import { useAuthStore } from '@/stores/authStore'
import { http } from '@/lib/http'
import { errorMessage, isApiError } from '@/lib/errors'
import { formatDateTime } from '@/lib/format'
import { DESIGN_KIND, DESIGN_STATUS_STAFF, labelOf, lookup } from '@/components/admin/labels'
import { RealtimeStale, ToneBadge } from '@/components/admin/ui'
import { useAssignableUsers, useUserDirectory } from '@/components/admin/directory'
import { DESIGN_PATHS, revisionLimit } from './paths'
import type { DesignQueue } from './types'

const ACTIVE = ['NA_FILA', 'EM_DESIGN', 'ALTERACAO_SOLICITADA', 'EM_REVISAO', 'APROVADA'] as const

export default function DesignQueuePage() {
  const me = useAuthStore((s) => s.me)
  const qc = useQueryClient()
  const [mine, setMine] = React.useState(false)
  const [status, setStatus] = React.useState<string>('all')
  const params = { mine: mine || undefined, status: status === 'all' ? undefined : status }
  const q = useQuery({ queryKey: ['design', 'queue', params], queryFn: () => http.get<DesignQueue>('/design/queue', { query: params }) })
  const designers = useAssignableUsers('design', 'design.work', '/design/designers')
  const directory = useUserDirectory()
  const assign = useMutation({
    mutationFn: ({ id, designer }: { id: string; designer: string | null }) => http.post(`/design/requests/${id}/assign`, { designer_id: designer }),
    onSuccess: () => {
      toast.success('Pedido atribuído.')
      void qc.invalidateQueries({ queryKey: ['design'] })
    },
    onError: (e) => toast.error(errorMessage(e)),
  })

  if (q.isLoading) return <PageSkeleton />
  if (q.error && isApiError(q.error) && q.error.status === 403) return <NoPermission permission="design.work" />
  if (q.error || !q.data) return <ErrorState error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />
  const data = q.data
  const nameOf = (id: string | null) =>
    !id ? null : id === me?.user.id ? 'Você' : directory.nameOf(id) ?? designers.data?.users.find((u) => u.id === id)?.name ?? 'Designer'

  return (
    <>
      <PageHeader title="Fila de produção" description={`${data.unassigned} pedido(s) sem responsável`} />
      <div className="flex flex-col gap-4">
        <RealtimeStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />
        <ul className="m-0 grid list-none grid-cols-2 gap-3 p-0 md:grid-cols-5" aria-label="Pedidos por etapa">
          {ACTIVE.map((s) => (
            <li key={s} className="flex flex-col gap-1 rounded-xl border border-border bg-surface p-4">
              <span className="text-xs text-text-muted">{lookup(DESIGN_STATUS_STAFF, s).label}</span>
              <span className="text-kpi-secondary tabular-nums text-text">{data.counts[s] ?? 0}</span>
            </li>
          ))}
        </ul>
        <Card>
          <div className="flex flex-wrap items-center gap-3">
            <ToggleGroup type="single" aria-label="Responsável" value={mine ? 'mine' : 'all'} onValueChange={(v) => v && setMine(v === 'mine')}>
              <ToggleGroupItem value="all">Todos</ToggleGroupItem>
              <ToggleGroupItem value="mine">Meus pedidos</ToggleGroupItem>
            </ToggleGroup>
            <Select value={status} onValueChange={setStatus}>
              <SelectTrigger className="w-full sm:w-64" aria-label="Etapa">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  <SelectItem value="all">Todas as etapas ativas</SelectItem>
                  {ACTIVE.map((s) => (
                    <SelectItem key={s} value={s}>
                      {lookup(DESIGN_STATUS_STAFF, s).label}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
          </div>
          {data.items.length ? (
            <div className="overflow-x-auto">
              <Table>
                <caption className="sr-only">Fila de produção</caption>
                <TableHeader>
                  <TableRow>
                    <TableHead scope="col">Pedido</TableHead>
                    <TableHead scope="col">Etapa</TableHead>
                    <TableHead scope="col">Revisões</TableHead>
                    <TableHead scope="col">Prazo</TableHead>
                    <TableHead scope="col">Responsável</TableHead>
                    <TableHead scope="col" className="text-right">
                      Ações
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.items.map((r) => (
                    <TableRow key={r.id}>
                      <TableCell>
                        <Link to={DESIGN_PATHS.rd.detail(r.id)} className="font-semibold text-link underline-offset-4 hover:underline">
                          {r.code} · {r.title}
                        </Link>
                        <span className="block text-xs text-text-muted">
                          {labelOf(DESIGN_KIND, r.kind)} · {r.orientation === 'VERTICAL' ? '1080×1920' : '1920×1080'} · {r.media_type === 'VIDEO' ? `vídeo ${r.duration_s}s` : 'imagem'}
                        </span>
                      </TableCell>
                      <TableCell>
                        <ToneBadge value={lookup(DESIGN_STATUS_STAFF, r.status)} />
                      </TableCell>
                      <TableCell className="tabular-nums">
                        {r.revisions_used}/{revisionLimit(r)}
                      </TableCell>
                      <TableCell className="tabular-nums">
                        {r.due_at && new Date(r.due_at).getTime() < Date.now() && r.status !== 'APROVADA' ? <Badge tone="danger">Atrasado</Badge> : null} {formatDateTime(r.due_at)}
                      </TableCell>
                      <TableCell>{nameOf(r.assigned_to) ?? <Badge tone="warning">Sem responsável</Badge>}</TableCell>
                      <TableCell className="text-right">
                        <div className="flex flex-wrap justify-end gap-1">
                          {r.assigned_to !== me?.user.id ? (
                            <Button variant="ghost" onClick={() => assign.mutate({ id: r.id, designer: null })} disabled={assign.isPending}>
                              <UserCheck aria-hidden="true" />
                              Assumir
                            </Button>
                          ) : null}
                          {designers.data?.users.length ? (
                            <Select value={r.assigned_to ?? ''} onValueChange={(v) => assign.mutate({ id: r.id, designer: v })}>
                              <SelectTrigger className="w-44" aria-label={`Atribuir ${r.code} a`}>
                                <SelectValue placeholder="Atribuir a…" />
                              </SelectTrigger>
                              <SelectContent>
                                <SelectGroup>
                                  {designers.data.users.map((u) => (
                                    <SelectItem key={u.id} value={u.id}>
                                      {u.name}
                                      {typeof u.load === 'number' ? ` · ${u.load} pedido(s) ativo(s)` : ''}
                                    </SelectItem>
                                  ))}
                                </SelectGroup>
                              </SelectContent>
                            </Select>
                          ) : null}
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          ) : (
            <EmptyState size="compact" headingLevel="h3" icon={ListOrdered} title="Fila vazia." description="Pedidos pagos (ou negociados/isentos) entram aqui automaticamente." />
          )}
        </Card>
      </div>
    </>
  )
}
