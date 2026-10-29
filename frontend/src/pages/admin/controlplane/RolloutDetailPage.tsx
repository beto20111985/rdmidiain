/**
 * Detalhe do rollout — GET /control-plane/rollouts/{id} (+ eventos): progresso por alvo,
 * despachar, pausar/retomar/cancelar, avançar SIMULAÇÃO (somente executor de simulação)
 * e rollback (gera um rollout inverso). Histórico append-only em /control-plane/events.
 */
import * as React from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { FlaskConical, Pause, Play, RefreshCw, Send, Undo2, XCircle } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Progress } from '@/components/ui/progress'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ErrorState, NoPermission, PageSkeleton, UpdatedAt } from '@/components/patterns/states'
import { ConfirmDialog } from '@/components/patterns/confirm-dialog'
import { useCan } from '@/hooks/usePermissions'
import { http } from '@/lib/http'
import { errorMessage, isApiError } from '@/lib/errors'
import { formatDateTime } from '@/lib/format'
import { CP_ROLLOUT_STATUS, CP_TARGET_KIND, CP_TARGET_STATUS, labelOf, lookup } from '@/components/admin/labels'
import { KeyValueList, MobileMeta, ReasonDialog, ToneBadge } from '@/components/admin/ui'
import { ExternalExecutorNotice } from './ExternalExecutorNotice'
import type { CpEvent, CpInstance, CpRelease, CpRollout } from './types'

const DONE = new Set(['CONCLUIDO', 'SIMULADO', 'FALHOU', 'CANCELADO', 'BLOQUEADO_DEPENDENCIA'])

export default function RolloutDetailPage() {
  const { id = '' } = useParams()
  const can = useCan()
  const manage = can('control_plane.manage')
  const navigate = useNavigate()
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['control-plane', 'rollout', id], queryFn: () => http.get<CpRollout>(`/control-plane/rollouts/${id}`), enabled: !!id })
  const events = useQuery({
    queryKey: ['control-plane', 'rollout', id, 'events'],
    queryFn: () => http.get<{ items: CpEvent[] }>('/control-plane/events', { query: { rollout_id: id, page_size: 100 } }).then((r) => r.items),
    enabled: !!id,
  })
  const instances = useQuery({ queryKey: ['control-plane', 'instances'], queryFn: () => http.get<{ items: CpInstance[] }>('/control-plane/instances').then((r) => r.items) })
  const releases = useQuery({ queryKey: ['control-plane', 'releases'], queryFn: () => http.get<{ items: CpRelease[] }>('/control-plane/releases').then((r) => r.items) })
  const [busy, setBusy] = React.useState<string | null>(null)
  const [cancelOpen, setCancelOpen] = React.useState(false)
  const [rollbackOpen, setRollbackOpen] = React.useState(false)
  const [rbConc, setRbConc] = React.useState('')

  if (q.isLoading) return <PageSkeleton />
  if (q.error || !q.data) {
    if (isApiError(q.error) && q.error.status === 403) return <NoPermission permission="control_plane.view" />
    return <ErrorState error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />
  }
  const r = q.data
  const version = releases.data?.find((x) => x.id === r.release_id)?.version ?? '—'
  const instName = (iid: string) => instances.data?.find((i) => i.id === iid)?.name ?? `${iid.slice(0, 8)}…`
  const targets = r.targets ?? []
  const done = targets.filter((t) => DONE.has(t.status)).length
  const pct = targets.length ? Math.round((done / targets.length) * 100) : 0
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ['control-plane'] })
  }
  const act = async (label: string, fn: () => Promise<unknown>, ok: string) => {
    setBusy(label)
    try {
      await fn()
      refresh()
      toast.success(ok)
    } catch (e) {
      toast.error(errorMessage(e))
    } finally {
      setBusy(null)
    }
  }
  const active = r.status === 'PENDENTE' || r.status === 'EM_ANDAMENTO'

  const headerMeta = (
    <span className="inline-flex flex-wrap items-center gap-2">
      <ToneBadge value={lookup(CP_ROLLOUT_STATUS, r.status)} />
      {r.simulated ? (
        <Badge tone="info">
          <FlaskConical aria-hidden="true" />
          SIMULAÇÃO
        </Badge>
      ) : null}
      {r.is_rollback ? <Badge tone="warning">Rollback</Badge> : null}
    </span>
  )

  return (
    <>
      <PageHeader
        title={`Rollout ${version}`}
        description={headerMeta}
        breadcrumbs={[{ label: 'Control plane', to: '/control-plane?aba=rollouts' }, { label: `Rollout ${version}` }]}
        actions={
          <Button variant="secondary" onClick={refresh} loading={q.isFetching}>
            <RefreshCw aria-hidden="true" />
            Atualizar
          </Button>
        }
      />
      <div className="flex flex-col gap-4 xl:gap-6">
        <MobileMeta>{headerMeta}</MobileMeta>
        {r.simulated ? (
          <Alert variant="info">
            <FlaskConical aria-hidden="true" />
            <div className="flex flex-col gap-1">
              <AlertTitle>SIMULAÇÃO LOCAL</AlertTitle>
              <AlertDescription>
                Nenhuma instância é atualizada de verdade. Os alvos avançam só quando você clica em “Avançar simulação”, e a versão reportada pelo
                heartbeat das instâncias não muda.
              </AlertDescription>
            </div>
          </Alert>
        ) : (
          <ExternalExecutorNotice />
        )}
        <UpdatedAt at={q.dataUpdatedAt} />
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-12 xl:gap-6">
          <Card className="xl:col-span-8">
            <CardHeader>
              <div className="flex flex-col gap-1">
                <CardTitle>Progresso</CardTitle>
                <CardDescription>
                  {done} de {targets.length} alvo(s) finalizado(s) · até {r.max_concurrency} por vez
                </CardDescription>
              </div>
            </CardHeader>
            <Progress value={pct} aria-label={`Progresso do rollout: ${pct}%`} />
            {targets.length ? (
              <div className="overflow-x-auto">
                <Table>
                  <caption className="sr-only">Alvos do rollout</caption>
                  <TableHeader>
                    <TableRow>
                      <TableHead scope="col">#</TableHead>
                      <TableHead scope="col">Instância</TableHead>
                      <TableHead scope="col">Versão</TableHead>
                      <TableHead scope="col">Situação</TableHead>
                      <TableHead scope="col">Tentativas</TableHead>
                      <TableHead scope="col">Detalhe</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {targets.map((t) => (
                      <TableRow key={t.id}>
                        <TableCell className="tabular-nums">{t.position}</TableCell>
                        <TableCell className="font-semibold">{instName(t.instance_id)}</TableCell>
                        <TableCell className="tabular-nums">
                          {t.from_version ?? '—'} → {t.to_version ?? '—'}
                        </TableCell>
                        <TableCell>
                          <ToneBadge value={lookup(CP_TARGET_STATUS, t.status)} />
                        </TableCell>
                        <TableCell className="tabular-nums">{t.attempt}</TableCell>
                        <TableCell className="max-w-[260px] text-xs text-text-muted">
                          {t.detail ?? '—'}
                          {t.finished_at ? <span className="block">{formatDateTime(t.finished_at)}</span> : null}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            ) : (
              <p className="text-sm text-text-muted">Nenhum alvo neste rollout.</p>
            )}
          </Card>
          <div className="flex flex-col gap-4 xl:col-span-4">
            <Card>
              <CardHeader>
                <CardTitle>Configuração</CardTitle>
              </CardHeader>
              <KeyValueList
                className="md:grid-cols-1"
                items={[
                  { label: 'Alvo', value: labelOf(CP_TARGET_KIND, r.target_kind) },
                  { label: 'Em caso de falha', value: r.failure_policy === 'PARAR' ? 'Parar o rollout' : 'Continuar com as demais' },
                  { label: 'Motivo', value: r.reason },
                  { label: 'Criado', value: formatDateTime(r.created_at) },
                  { label: 'Iniciado', value: formatDateTime(r.started_at) },
                  { label: 'Finalizado', value: formatDateTime(r.finished_at) },
                ]}
              />
            </Card>
            {manage ? (
              <Card>
                <CardHeader>
                  <CardTitle>Ações</CardTitle>
                </CardHeader>
                <div className="flex flex-col gap-2">
                  {r.status === 'PENDENTE' ? (
                    <Button loading={busy === 'dispatch'} onClick={() => void act('dispatch', () => http.post(`/control-plane/rollouts/${r.id}/dispatch`), 'Rollout despachado.')}>
                      <Send aria-hidden="true" />
                      Iniciar
                    </Button>
                  ) : null}
                  {r.simulated && active ? (
                    <Button
                      variant="secondary"
                      loading={busy === 'sim'}
                      onClick={() => void act('sim', () => http.post(`/control-plane/rollouts/${r.id}/simulate-step`, { fail_instance_ids: [] }), 'Simulação avançada.')}
                    >
                      <FlaskConical aria-hidden="true" />
                      Avançar simulação
                    </Button>
                  ) : null}
                  {r.status === 'EM_ANDAMENTO' ? (
                    <Button variant="secondary" loading={busy === 'pause'} onClick={() => void act('pause', () => http.post(`/control-plane/rollouts/${r.id}/pause`), 'Rollout pausado.')}>
                      <Pause aria-hidden="true" />
                      Pausar
                    </Button>
                  ) : null}
                  {r.status === 'PAUSADO' ? (
                    <Button variant="secondary" loading={busy === 'resume'} onClick={() => void act('resume', () => http.post(`/control-plane/rollouts/${r.id}/resume`), 'Rollout retomado.')}>
                      <Play aria-hidden="true" />
                      Retomar
                    </Button>
                  ) : null}
                  {active || r.status === 'PAUSADO' ? (
                    <Button variant="destructive" onClick={() => setCancelOpen(true)}>
                      <XCircle aria-hidden="true" />
                      Cancelar rollout
                    </Button>
                  ) : null}
                  {!r.is_rollback && !active && r.status !== 'PAUSADO' ? (
                    <Button variant="destructive" onClick={() => setRollbackOpen(true)}>
                      <Undo2 aria-hidden="true" />
                      Rollback
                    </Button>
                  ) : null}
                </div>
              </Card>
            ) : null}
          </div>
        </div>
        <Card>
          <CardHeader>
            <CardTitle>Histórico</CardTitle>
          </CardHeader>
          {events.isLoading ? (
            <PageSkeleton label="Carregando histórico…" />
          ) : events.error ? (
            <ErrorState error={events.error} onRetry={() => void events.refetch()} size="compact" />
          ) : events.data?.length ? (
            <ol className="m-0 flex list-none flex-col gap-2 p-0 text-sm">
              {events.data.map((e) => (
                <li key={e.id} className="flex flex-col rounded-md border border-border p-3">
                  <span className="font-semibold">{e.type}</span>
                  <span className="text-xs text-text-muted">
                    {formatDateTime(e.created_at)}
                    {e.instance_id ? ` · ${instName(e.instance_id)}` : ''}
                  </span>
                </li>
              ))}
            </ol>
          ) : (
            <p className="text-sm text-text-muted">Sem eventos registrados.</p>
          )}
        </Card>
      </div>
      <ConfirmDialog
        open={cancelOpen}
        onOpenChange={setCancelOpen}
        title="Cancelar este rollout?"
        description="Alvos ainda na fila não serão processados. Alvos já concluídos permanecem como estão."
        confirmLabel="Cancelar rollout"
        destructive
        onConfirm={async () => {
          await http.post(`/control-plane/rollouts/${r.id}/cancel`)
          refresh()
          toast.success('Rollout cancelado.')
        }}
      />
      <ReasonDialog
        open={rollbackOpen}
        onOpenChange={setRollbackOpen}
        title={`Rollback do rollout ${version}?`}
        description="Cria um novo rollout que devolve cada instância à versão anterior, na ordem inversa."
        minLength={3}
        maxLength={500}
        confirmLabel="Criar rollback"
        destructive
        onConfirm={async (reason) => {
          const n = Number(rbConc)
          const rb = await http.post<CpRollout>(`/control-plane/rollouts/${r.id}/rollback`, {
            reason,
            max_concurrency: Number.isInteger(n) && n >= 1 && n <= 20 ? n : undefined,
            start: true,
          })
          refresh()
          toast.success('Rollback criado.')
          navigate(`/control-plane/rollouts/${rb.id}`)
        }}
      >
        <Field>
          <FieldLabel>Máximo simultâneo (opcional)</FieldLabel>
          <Input type="number" min={1} max={20} value={rbConc} onChange={(e) => setRbConc(e.target.value)} className="w-28" />
          <FieldDescription>Vazio = mesmo limite do rollout original.</FieldDescription>
        </Field>
      </ReasonDialog>
    </>
  )
}
