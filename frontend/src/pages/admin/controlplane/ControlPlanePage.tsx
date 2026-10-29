/**
 * Control plane (`control_plane.view|manage`) — instâncias (saúde, último heartbeat,
 * versão), grupos, releases (DEV → validada → publicada) e rollouts (uma/grupo/todas,
 * versão, limite de concorrência). A execução real do deploy (Portainer) é dependência
 * externa: o executor NULO fica "bloqueado" e a simulação local é sempre rotulada como
 * SIMULAÇÃO. Não há evento de tempo real deste domínio: "Atualizar" refaz a consulta.
 */
import * as React from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { CheckCircle2, Copy, FlaskConical, Plus, RefreshCw, Rocket, Send, Server } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Checkbox } from '@/components/ui/checkbox'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Field, FieldDescription, FieldGroup, FieldLabel } from '@/components/ui/field'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Sheet, SheetBody, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { KpiCard } from '@/components/patterns/kpi-card'
import { EmptyState, ErrorState, PageSkeleton, UpdatedAt } from '@/components/patterns/states'
import { ConfirmDialog } from '@/components/patterns/confirm-dialog'
import { useCan } from '@/hooks/usePermissions'
import { http, newRequestId } from '@/lib/http'
import { errorMessage } from '@/lib/errors'
import { formatDateTime, formatRelative } from '@/lib/format'
import {
  CP_HEALTH,
  CP_INSTANCE_KIND,
  CP_INSTANCE_STATUS,
  CP_RELEASE_STATUS,
  CP_ROLLOUT_STATUS,
  CP_TARGET_KIND,
  labelOf,
  lookup,
} from '@/components/admin/labels'
import { KeyValueList, ToneBadge } from '@/components/admin/ui'
import type { CpGroup, CpInstance, CpOverview, CpRelease, CpRollout } from './types'
import { ExternalExecutorNotice } from './ExternalExecutorNotice'

const TABS = ['visao', 'instancias', 'grupos', 'releases', 'rollouts'] as const
type Tab = (typeof TABS)[number]

export default function ControlPlanePage() {
  const can = useCan()
  const manage = can('control_plane.manage')
  const [sp, setSp] = useSearchParams()
  const raw = sp.get('aba')
  const tab: Tab = (TABS as readonly string[]).includes(raw ?? '') ? (raw as Tab) : 'visao'
  const qc = useQueryClient()
  const overview = useQuery({ queryKey: ['control-plane', 'overview'], queryFn: () => http.get<CpOverview>('/control-plane/overview') })
  const instances = useQuery({ queryKey: ['control-plane', 'instances'], queryFn: () => http.get<{ items: CpInstance[] }>('/control-plane/instances').then((r) => r.items) })
  const groups = useQuery({ queryKey: ['control-plane', 'groups'], queryFn: () => http.get<{ items: CpGroup[] }>('/control-plane/groups').then((r) => r.items) })
  const releases = useQuery({ queryKey: ['control-plane', 'releases'], queryFn: () => http.get<{ items: CpRelease[] }>('/control-plane/releases').then((r) => r.items) })
  const rollouts = useQuery({
    queryKey: ['control-plane', 'rollouts'],
    queryFn: () => http.get<{ items: CpRollout[]; total: number }>('/control-plane/rollouts', { query: { page_size: 50 } }),
  })
  const refreshing = overview.isFetching || instances.isFetching || releases.isFetching || rollouts.isFetching
  const refresh = () => void qc.invalidateQueries({ queryKey: ['control-plane'] })

  return (
    <>
      <PageHeader
        title="Control plane"
        description="Instâncias, versões e atualizações gerenciadas pela RD."
        actions={
          <Button variant="secondary" onClick={refresh} loading={refreshing}>
            <RefreshCw aria-hidden="true" />
            Atualizar
          </Button>
        }
      />
      <div className="flex flex-col gap-4">
        <ExternalExecutorNotice detail={overview.data?.execution.detail} />
        <UpdatedAt at={overview.dataUpdatedAt || null} />
        <Tabs
          value={tab}
          onValueChange={(v) =>
            setSp(
              (prev) => {
                const n = new URLSearchParams(prev)
                if (v === 'visao') n.delete('aba')
                else n.set('aba', v)
                return n
              },
              { replace: true },
            )
          }
        >
          <TabsList aria-label="Seções do control plane">
            <TabsTrigger value="visao">Visão geral</TabsTrigger>
            <TabsTrigger value="instancias">Instâncias</TabsTrigger>
            <TabsTrigger value="grupos">Grupos</TabsTrigger>
            <TabsTrigger value="releases">Releases</TabsTrigger>
            <TabsTrigger value="rollouts">Rollouts</TabsTrigger>
          </TabsList>
          <TabsContent value="visao">
            <OverviewTab q={overview} />
          </TabsContent>
          <TabsContent value="instancias">
            <InstancesTab q={instances} groups={groups.data ?? []} manage={manage} />
          </TabsContent>
          <TabsContent value="grupos">
            <GroupsTab q={groups} instances={instances.data ?? []} manage={manage} />
          </TabsContent>
          <TabsContent value="releases">
            <ReleasesTab q={releases} instances={instances.data ?? []} manage={manage} />
          </TabsContent>
          <TabsContent value="rollouts">
            <RolloutsTab q={rollouts} releases={releases.data ?? []} instances={instances.data ?? []} groups={groups.data ?? []} manage={manage} />
          </TabsContent>
        </Tabs>
      </div>
    </>
  )
}

type Q<T> = { data?: T; isLoading: boolean; error: unknown; refetch: () => unknown; isFetching: boolean }

function Loadable<T>({ q, children, empty }: { q: Q<T>; children: (d: T) => React.ReactNode; empty?: (d: T) => boolean }) {
  if (q.isLoading) return <PageSkeleton />
  if (q.error || q.data === undefined) return <ErrorState error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />
  if (empty?.(q.data)) return null
  return <>{children(q.data)}</>
}

function OverviewTab({ q }: { q: Q<CpOverview> }) {
  return (
    <Loadable q={q}>
      {(o) => (
        <div className="flex flex-col gap-4 xl:gap-6">
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4 xl:gap-6">
            <KpiCard label="Instâncias" value={o.instances} />
            <KpiCard label="Desatualizadas" value={o.outdated} description="Fora da última versão publicada (exceto DEV)." />
            <KpiCard label="Rollouts ativos" value={o.active_rollouts} />
            <Card>
              <p className="text-xs font-semibold uppercase tracking-metric text-text-muted">Última versão publicada</p>
              <p className="text-kpi-secondary text-text">{o.latest_published_version ?? '—'}</p>
            </Card>
          </div>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:gap-6">
            <Card>
              <CardHeader>
                <CardTitle>Saúde (por heartbeat)</CardTitle>
              </CardHeader>
              <DistList map={o.by_health} label={(k) => lookup(CP_HEALTH, k).label} />
            </Card>
            <Card>
              <CardHeader>
                <CardTitle>Versões em uso</CardTitle>
              </CardHeader>
              <DistList map={o.by_version} label={(k) => (k === 'desconhecida' ? 'Desconhecida (sem heartbeat)' : k)} />
            </Card>
          </div>
        </div>
      )}
    </Loadable>
  )
}

function DistList({ map, label }: { map: Record<string, number>; label: (k: string) => string }) {
  const entries = Object.entries(map).sort((a, b) => b[1] - a[1])
  if (!entries.length) return <p className="text-sm text-text-muted">Nenhuma instância cadastrada.</p>
  return (
    <ul className="m-0 flex list-none flex-col divide-y divide-border p-0">
      {entries.map(([k, n]) => (
        <li key={k} className="flex items-center justify-between py-2 text-sm">
          <span>{label(k)}</span>
          <span className="font-semibold tabular-nums">{n}</span>
        </li>
      ))}
    </ul>
  )
}

function InstancesTab({ q, groups, manage }: { q: Q<CpInstance[]>; groups: CpGroup[]; manage: boolean }) {
  const [creating, setCreating] = React.useState(false)
  const [detailId, setDetailId] = React.useState<string | null>(null)
  const groupName = (id: string | null) => groups.find((g) => g.id === id)?.name ?? '—'
  return (
    <Card>
      <CardHeader>
        <CardTitle>Instâncias</CardTitle>
        {manage ? (
          <Button onClick={() => setCreating(true)}>
            <Plus aria-hidden="true" />
            Nova instância
          </Button>
        ) : null}
      </CardHeader>
      <Loadable q={q}>
        {(items) =>
          items.length ? (
            <div className="overflow-x-auto">
              <Table>
                <caption className="sr-only">Instâncias</caption>
                <TableHeader>
                  <TableRow>
                    <TableHead scope="col">Instância</TableHead>
                    <TableHead scope="col">Tipo</TableHead>
                    <TableHead scope="col">Grupo</TableHead>
                    <TableHead scope="col">Saúde</TableHead>
                    <TableHead scope="col">Versão (heartbeat)</TableHead>
                    <TableHead scope="col">Último heartbeat</TableHead>
                    <TableHead scope="col">Situação</TableHead>
                    <TableHead scope="col" className="text-right">
                      Ações
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {items.map((i) => (
                    <TableRow key={i.id}>
                      <TableCell>
                        <span className="font-semibold">{i.name}</span>
                        <span className="block text-xs text-text-muted">
                          <code>{i.slug}</code>
                        </span>
                      </TableCell>
                      <TableCell>{labelOf(CP_INSTANCE_KIND, i.kind)}</TableCell>
                      <TableCell>{groupName(i.group_id)}</TableCell>
                      <TableCell>
                        <ToneBadge value={lookup(CP_HEALTH, i.health_status)} />
                      </TableCell>
                      <TableCell className="tabular-nums">{i.current_version ?? '—'}</TableCell>
                      <TableCell className="tabular-nums">
                        {i.last_heartbeat_at ? (
                          <time dateTime={i.last_heartbeat_at} title={formatDateTime(i.last_heartbeat_at)}>
                            {formatRelative(i.last_heartbeat_at)}
                          </time>
                        ) : (
                          'Nunca'
                        )}
                      </TableCell>
                      <TableCell>
                        <ToneBadge value={lookup(CP_INSTANCE_STATUS, i.status)} />
                      </TableCell>
                      <TableCell className="text-right">
                        <Button variant="ghost" onClick={() => setDetailId(i.id)}>
                          Detalhes
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          ) : (
            <EmptyState size="compact" headingLevel="h3" icon={Server} title="Nenhuma instância cadastrada." description="Cadastre a instância RD de produção e a DEV para validar versões." />
          )
        }
      </Loadable>
      <InstanceCreateDialog open={creating} onOpenChange={setCreating} groups={groups} />
      <InstanceSheet id={detailId} onClose={() => setDetailId(null)} groups={groups} manage={manage} />
    </Card>
  )
}

function TokenOnce({ token }: { token: string }) {
  return (
    <Alert variant="warning">
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <AlertTitle>Token da instância — exibido uma única vez</AlertTitle>
        <AlertDescription className="flex flex-col gap-2">
          <span>Copie agora e configure na instância (heartbeat). Só o hash fica guardado; para um novo token, use “Gerar novo token”.</span>
          <code className="break-all rounded-md bg-field p-2 text-xs text-text">{token}</code>
          <Button
            variant="secondary"
            className="w-fit"
            onClick={() => {
              void navigator.clipboard?.writeText(token).then(
                () => toast.success('Token copiado.'),
                () => toast.error('Não foi possível copiar; selecione o texto.'),
              )
            }}
          >
            <Copy aria-hidden="true" />
            Copiar token
          </Button>
        </AlertDescription>
      </div>
    </Alert>
  )
}

function InstanceCreateDialog({ open, onOpenChange, groups }: { open: boolean; onOpenChange: (o: boolean) => void; groups: CpGroup[] }) {
  const qc = useQueryClient()
  const [slug, setSlug] = React.useState('')
  const [name, setName] = React.useState('')
  const [kind, setKind] = React.useState('RD_PRODUCAO')
  const [baseUrl, setBaseUrl] = React.useState('')
  const [groupId, setGroupId] = React.useState('none')
  const [notes, setNotes] = React.useState('')
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)
  const [token, setToken] = React.useState<string | null>(null)
  React.useEffect(() => {
    if (!open) {
      setSlug('')
      setName('')
      setKind('RD_PRODUCAO')
      setBaseUrl('')
      setGroupId('none')
      setNotes('')
      setError(null)
      setToken(null)
    }
  }, [open])
  const ok = slug.trim().length >= 2 && name.trim().length >= 2
  return (
    <Dialog open={open} onOpenChange={(o) => (!pending ? onOpenChange(o) : undefined)}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>Nova instância</DialogTitle>
          <DialogDescription>Cada instância roda em stack própria; aqui ela é registrada para receber versões e enviar heartbeat.</DialogDescription>
        </DialogHeader>
        {token ? (
          <TokenOnce token={token} />
        ) : (
          <form
            noValidate
            className="flex flex-col gap-5"
            onSubmit={async (e) => {
              e.preventDefault()
              if (!ok) return
              setPending(true)
              setError(null)
              try {
                const r = await http.post<CpInstance>('/control-plane/instances', {
                  slug: slug.trim(),
                  name: name.trim(),
                  kind,
                  base_url: baseUrl.trim() || null,
                  group_id: groupId === 'none' ? null : groupId,
                  notes: notes.trim() || null,
                })
                void qc.invalidateQueries({ queryKey: ['control-plane'] })
                setToken(r.instance_token ?? null)
                toast.success('Instância cadastrada.')
              } catch (err) {
                setError(errorMessage(err))
              } finally {
                setPending(false)
              }
            }}
          >
            <FieldGroup>
              <Field required>
                <FieldLabel>Identificador</FieldLabel>
                <Input value={slug} onChange={(e) => setSlug(e.target.value.toLowerCase())} spellCheck={false} />
                <FieldDescription>Ex.: rd-producao, dev-vps, wl-rede-sul.</FieldDescription>
              </Field>
              <Field required>
                <FieldLabel>Nome</FieldLabel>
                <Input value={name} onChange={(e) => setName(e.target.value)} />
              </Field>
              <Field>
                <FieldLabel>Tipo</FieldLabel>
                <Select value={kind} onValueChange={setKind}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      {Object.entries(CP_INSTANCE_KIND).map(([k, l]) => (
                        <SelectItem key={k} value={k}>
                          {l}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  </SelectContent>
                </Select>
              </Field>
              <Field>
                <FieldLabel>URL base</FieldLabel>
                <Input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://subdominio.exemplo" />
              </Field>
              <Field>
                <FieldLabel>Grupo</FieldLabel>
                <Select value={groupId} onValueChange={setGroupId}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      <SelectItem value="none">Sem grupo</SelectItem>
                      {groups.map((g) => (
                        <SelectItem key={g.id} value={g.id}>
                          {g.name}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  </SelectContent>
                </Select>
              </Field>
              <Field>
                <FieldLabel>Observações</FieldLabel>
                <Textarea value={notes} onChange={(e) => setNotes(e.target.value)} />
              </Field>
            </FieldGroup>
            {error ? (
              <Alert variant="danger">
                <AlertDescription className="text-text">{error}</AlertDescription>
              </Alert>
            ) : null}
            <DialogFooter>
              <Button variant="secondary" onClick={() => onOpenChange(false)} disabled={pending}>
                Cancelar
              </Button>
              <Button type="submit" loading={pending} disabled={!ok}>
                Cadastrar instância
              </Button>
            </DialogFooter>
          </form>
        )}
        {token ? (
          <DialogFooter>
            <Button onClick={() => onOpenChange(false)}>Concluir</Button>
          </DialogFooter>
        ) : null}
      </DialogContent>
    </Dialog>
  )
}

function InstanceSheet({ id, onClose, groups, manage }: { id: string | null; onClose: () => void; groups: CpGroup[]; manage: boolean }) {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['control-plane', 'instance', id], queryFn: () => http.get<CpInstance>(`/control-plane/instances/${id}`), enabled: !!id })
  const [status, setStatus] = React.useState<string>('')
  const [groupId, setGroupId] = React.useState('none')
  const [notes, setNotes] = React.useState('')
  const [saving, setSaving] = React.useState(false)
  const [rotating, setRotating] = React.useState(false)
  const [token, setToken] = React.useState<string | null>(null)
  React.useEffect(() => {
    if (q.data) {
      setStatus(q.data.status)
      setGroupId(q.data.group_id ?? 'none')
      setNotes(q.data.notes ?? '')
    }
    if (!id) setToken(null)
  }, [q.data, id])
  const i = q.data
  return (
    <Sheet open={!!id} onOpenChange={(o) => !o && onClose()}>
      <SheetContent className="max-w-2xl">
        <SheetHeader>
          <SheetTitle>{i?.name ?? 'Instância'}</SheetTitle>
          <SheetDescription>{i ? `${i.slug} · ${labelOf(CP_INSTANCE_KIND, i.kind)}` : 'Carregando…'}</SheetDescription>
        </SheetHeader>
        <SheetBody className="flex flex-col gap-6 py-6">
          {q.isLoading ? (
            <PageSkeleton />
          ) : q.error || !i ? (
            <ErrorState error={q.error} onRetry={() => void q.refetch()} size="compact" />
          ) : (
            <>
              <KeyValueList
                items={[
                  { label: 'Saúde', value: <ToneBadge value={lookup(CP_HEALTH, i.health_status)} /> },
                  { label: 'Versão atual (heartbeat)', value: i.current_version },
                  { label: 'Última versão implantada', value: i.last_deployed_version },
                  { label: 'Último heartbeat', value: formatDateTime(i.last_heartbeat_at) },
                  { label: 'URL base', value: i.base_url },
                  { label: 'Detalhes de saúde', value: Object.keys(i.health ?? {}).length ? <code className="break-all text-xs">{JSON.stringify(i.health)}</code> : null, wide: true },
                ]}
              />
              {token ? <TokenOnce token={token} /> : null}
              {manage ? (
                <form
                  noValidate
                  className="flex flex-col gap-4"
                  onSubmit={async (e) => {
                    e.preventDefault()
                    setSaving(true)
                    try {
                      await http.patch(`/control-plane/instances/${i.id}`, { status, group_id: groupId === 'none' ? null : groupId, notes: notes.trim() || null })
                      void qc.invalidateQueries({ queryKey: ['control-plane'] })
                      toast.success('Instância atualizada.')
                    } catch (err) {
                      toast.error(errorMessage(err))
                    } finally {
                      setSaving(false)
                    }
                  }}
                >
                  <h3 className="text-title-card text-text">Gerenciar</h3>
                  <Field>
                    <FieldLabel id="inst-status">Situação</FieldLabel>
                    <ToggleGroup type="single" aria-labelledby="inst-status" value={status} onValueChange={(v) => v && setStatus(v)} className="w-fit">
                      <ToggleGroupItem value="ATIVA">Ativa</ToggleGroupItem>
                      <ToggleGroupItem value="MANUTENCAO">Manutenção</ToggleGroupItem>
                      <ToggleGroupItem value="INATIVA">Inativa</ToggleGroupItem>
                    </ToggleGroup>
                  </Field>
                  <Field>
                    <FieldLabel>Grupo</FieldLabel>
                    <Select value={groupId} onValueChange={setGroupId}>
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectGroup>
                          <SelectItem value="none">Sem grupo</SelectItem>
                          {groups.map((g) => (
                            <SelectItem key={g.id} value={g.id}>
                              {g.name}
                            </SelectItem>
                          ))}
                        </SelectGroup>
                      </SelectContent>
                    </Select>
                  </Field>
                  <Field>
                    <FieldLabel>Observações</FieldLabel>
                    <Textarea value={notes} onChange={(e) => setNotes(e.target.value)} />
                  </Field>
                  <div className="flex flex-wrap justify-end gap-2">
                    <Button variant="secondary" onClick={() => setRotating(true)}>
                      Gerar novo token
                    </Button>
                    <Button type="submit" loading={saving}>
                      Salvar
                    </Button>
                  </div>
                </form>
              ) : null}
              <section aria-labelledby="inst-events" className="flex flex-col gap-2">
                <h3 id="inst-events" className="text-title-card text-text">
                  Histórico recente
                </h3>
                {i.events?.length ? (
                  <ol className="m-0 flex list-none flex-col gap-2 p-0 text-sm">
                    {i.events.map((ev, idx) => (
                      <li key={idx} className="flex flex-col rounded-md border border-border p-2">
                        <span className="font-semibold">{ev.type}</span>
                        <span className="text-xs text-text-muted">{formatDateTime(ev.created_at)}</span>
                      </li>
                    ))}
                  </ol>
                ) : (
                  <p className="text-sm text-text-muted">Sem eventos.</p>
                )}
              </section>
            </>
          )}
        </SheetBody>
      </SheetContent>
      <ConfirmDialog
        open={rotating}
        onOpenChange={setRotating}
        title="Gerar novo token da instância?"
        description="O token atual deixa de funcionar imediatamente; a instância só volta a enviar heartbeat com o novo token."
        confirmLabel="Gerar novo token"
        destructive
        onConfirm={async () => {
          if (!i) return
          const r = await http.post<{ instance_token: string }>(`/control-plane/instances/${i.id}/rotate-token`)
          setToken(r.instance_token)
        }}
      />
    </Sheet>
  )
}

function GroupsTab({ q, instances, manage }: { q: Q<CpGroup[]>; instances: CpInstance[]; manage: boolean }) {
  const qc = useQueryClient()
  const [name, setName] = React.useState('')
  const [description, setDescription] = React.useState('')
  const [pending, setPending] = React.useState(false)
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-12 xl:gap-6">
      <Card className="lg:col-span-8">
        <CardHeader>
          <CardTitle>Grupos de instâncias</CardTitle>
        </CardHeader>
        <Loadable q={q}>
          {(items) =>
            items.length ? (
              <ul className="m-0 flex list-none flex-col divide-y divide-border p-0">
                {items.map((g) => {
                  const members = instances.filter((i) => i.group_id === g.id)
                  return (
                    <li key={g.id} className="flex flex-col gap-1 py-3 first:pt-0">
                      <span className="font-semibold">{g.name}</span>
                      {g.description ? <span className="text-sm text-text-muted">{g.description}</span> : null}
                      <span className="text-xs text-text-muted">{members.length ? members.map((m) => m.name).join(', ') : 'Sem instâncias'}</span>
                    </li>
                  )
                })}
              </ul>
            ) : (
              <EmptyState size="compact" headingLevel="h3" title="Nenhum grupo." description="Agrupe instâncias para atualizar várias de uma vez." />
            )
          }
        </Loadable>
      </Card>
      {manage ? (
        <Card className="lg:col-span-4">
          <CardHeader>
            <CardTitle>Novo grupo</CardTitle>
          </CardHeader>
          <form
            noValidate
            className="flex flex-col gap-4"
            onSubmit={async (e) => {
              e.preventDefault()
              if (name.trim().length < 2) return
              setPending(true)
              try {
                await http.post('/control-plane/groups', { name: name.trim(), description: description.trim() || null })
                setName('')
                setDescription('')
                void qc.invalidateQueries({ queryKey: ['control-plane', 'groups'] })
                toast.success('Grupo criado.')
              } catch (err) {
                toast.error(errorMessage(err))
              } finally {
                setPending(false)
              }
            }}
          >
            <Field required>
              <FieldLabel>Nome</FieldLabel>
              <Input value={name} onChange={(e) => setName(e.target.value)} />
            </Field>
            <Field>
              <FieldLabel>Descrição</FieldLabel>
              <Textarea value={description} onChange={(e) => setDescription(e.target.value)} />
            </Field>
            <Button type="submit" loading={pending} disabled={name.trim().length < 2}>
              Criar grupo
            </Button>
          </form>
        </Card>
      ) : null}
    </div>
  )
}

function ReleasesTab({ q, instances, manage }: { q: Q<CpRelease[]>; instances: CpInstance[]; manage: boolean }) {
  const qc = useQueryClient()
  const [creating, setCreating] = React.useState(false)
  const [validating, setValidating] = React.useState<CpRelease | null>(null)
  const [publishing, setPublishing] = React.useState<CpRelease | null>(null)
  const devs = instances.filter((i) => i.kind === 'DEV')
  const [devId, setDevId] = React.useState('')
  const invalidate = () => void qc.invalidateQueries({ queryKey: ['control-plane'] })
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Releases</CardTitle>
          <CardDescription>Fluxo: DEV (rascunho) → validada na instância DEV → publicada → rollout.</CardDescription>
        </div>
        {manage ? (
          <Button onClick={() => setCreating(true)}>
            <Plus aria-hidden="true" />
            Nova release
          </Button>
        ) : null}
      </CardHeader>
      <Loadable q={q}>
        {(items) =>
          items.length ? (
            <div className="overflow-x-auto">
              <Table>
                <caption className="sr-only">Releases</caption>
                <TableHeader>
                  <TableRow>
                    <TableHead scope="col">Versão</TableHead>
                    <TableHead scope="col">Situação</TableHead>
                    <TableHead scope="col">Imagem</TableHead>
                    <TableHead scope="col">Validada</TableHead>
                    <TableHead scope="col">Publicada</TableHead>
                    {manage ? (
                      <TableHead scope="col" className="text-right">
                        Ações
                      </TableHead>
                    ) : null}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {items.map((r) => (
                    <TableRow key={r.id}>
                      <TableCell>
                        <span className="font-semibold tabular-nums">{r.version}</span>
                        {r.changelog ? <span className="block max-w-[320px] truncate text-xs text-text-muted">{r.changelog}</span> : null}
                      </TableCell>
                      <TableCell>
                        <ToneBadge value={lookup(CP_RELEASE_STATUS, r.status)} />
                      </TableCell>
                      <TableCell className="max-w-[220px] break-all text-xs">{r.image_ref ?? '—'}</TableCell>
                      <TableCell className="tabular-nums">
                        {formatDateTime(r.validated_at)}
                        {r.validated_on_instance_id ? (
                          <span className="block text-xs text-text-muted">em {instances.find((i) => i.id === r.validated_on_instance_id)?.name ?? 'DEV'}</span>
                        ) : null}
                      </TableCell>
                      <TableCell className="tabular-nums">{formatDateTime(r.published_at)}</TableCell>
                      {manage ? (
                        <TableCell className="text-right">
                          {r.status === 'RASCUNHO' ? (
                            <Button
                              variant="ghost"
                              onClick={() => {
                                setDevId(devs[0]?.id ?? '')
                                setValidating(r)
                              }}
                            >
                              <CheckCircle2 aria-hidden="true" />
                              Validar na DEV
                            </Button>
                          ) : r.status === 'VALIDADA' ? (
                            <Button variant="ghost" onClick={() => setPublishing(r)}>
                              <Send aria-hidden="true" />
                              Publicar
                            </Button>
                          ) : null}
                        </TableCell>
                      ) : null}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          ) : (
            <EmptyState size="compact" headingLevel="h3" icon={Rocket} title="Nenhuma release." description="Registre a versão gerada no ambiente local para validá-la na DEV." />
          )
        }
      </Loadable>
      <ReleaseCreateDialog open={creating} onOpenChange={setCreating} onDone={invalidate} />
      <Dialog open={!!validating} onOpenChange={(o) => !o && setValidating(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Validar {validating?.version} na instância DEV</DialogTitle>
            <DialogDescription>Confirme que a versão foi testada na instância de desenvolvimento.</DialogDescription>
          </DialogHeader>
          <Field>
            <FieldLabel>Instância DEV</FieldLabel>
            <Select value={devId} onValueChange={setDevId}>
              <SelectTrigger>
                <SelectValue placeholder={devs.length ? 'Selecione' : 'Nenhuma instância DEV cadastrada'} />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  {devs.map((d) => (
                    <SelectItem key={d.id} value={d.id}>
                      {d.name} ({d.current_version ?? 'sem heartbeat'})
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
          </Field>
          <DialogFooter>
            <Button variant="secondary" onClick={() => setValidating(null)}>
              Cancelar
            </Button>
            <Button
              disabled={!devId}
              onClick={async () => {
                if (!validating) return
                try {
                  await http.post(`/control-plane/releases/${validating.id}/validate`, { dev_instance_id: devId })
                  invalidate()
                  toast.success('Release validada.')
                  setValidating(null)
                } catch (err) {
                  toast.error(errorMessage(err))
                }
              }}
            >
              Marcar como validada
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <ConfirmDialog
        open={!!publishing}
        onOpenChange={(o) => !o && setPublishing(null)}
        title={`Publicar a versão ${publishing?.version ?? ''}?`}
        description="Versões publicadas ficam disponíveis para rollout nas instâncias de clientes."
        confirmLabel="Publicar versão"
        onConfirm={async () => {
          if (!publishing) return
          await http.post(`/control-plane/releases/${publishing.id}/publish`)
          invalidate()
          toast.success('Versão publicada.')
        }}
      />
    </Card>
  )
}

function ReleaseCreateDialog({ open, onOpenChange, onDone }: { open: boolean; onOpenChange: (o: boolean) => void; onDone: () => void }) {
  const [version, setVersion] = React.useState('')
  const [image, setImage] = React.useState('')
  const [changelog, setChangelog] = React.useState('')
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)
  React.useEffect(() => {
    if (!open) {
      setVersion('')
      setImage('')
      setChangelog('')
      setError(null)
    }
  }, [open])
  return (
    <Dialog open={open} onOpenChange={(o) => (!pending ? onOpenChange(o) : undefined)}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>Nova release</DialogTitle>
          <DialogDescription>Entra como DEV (rascunho); nunca vai para clientes antes de validada e publicada.</DialogDescription>
        </DialogHeader>
        <form
          noValidate
          className="flex flex-col gap-5"
          onSubmit={async (e) => {
            e.preventDefault()
            setPending(true)
            setError(null)
            try {
              await http.post('/control-plane/releases', { version: version.trim(), image_ref: image.trim() || null, changelog: changelog.trim() || null })
              onDone()
              toast.success('Release registrada.')
              onOpenChange(false)
            } catch (err) {
              setError(errorMessage(err))
            } finally {
              setPending(false)
            }
          }}
        >
          <FieldGroup>
            <Field required>
              <FieldLabel>Versão</FieldLabel>
              <Input value={version} onChange={(e) => setVersion(e.target.value)} placeholder="1.4.0" />
              <FieldDescription>Versionamento semântico (ex.: 1.4.0).</FieldDescription>
            </Field>
            <Field>
              <FieldLabel>Referência da imagem</FieldLabel>
              <Input value={image} onChange={(e) => setImage(e.target.value)} spellCheck={false} />
            </Field>
            <Field>
              <FieldLabel>Notas da versão</FieldLabel>
              <Textarea value={changelog} onChange={(e) => setChangelog(e.target.value)} />
            </Field>
          </FieldGroup>
          {error ? (
            <Alert variant="danger">
              <AlertDescription className="text-text">{error}</AlertDescription>
            </Alert>
          ) : null}
          <DialogFooter>
            <Button variant="secondary" onClick={() => onOpenChange(false)} disabled={pending}>
              Cancelar
            </Button>
            <Button type="submit" loading={pending} disabled={version.trim().length < 5}>
              Registrar release
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function RolloutsTab({
  q,
  releases,
  instances,
  groups,
  manage,
}: {
  q: Q<{ items: CpRollout[]; total: number }>
  releases: CpRelease[]
  instances: CpInstance[]
  groups: CpGroup[]
  manage: boolean
}) {
  const [creating, setCreating] = React.useState(false)
  const versionOf = (id: string) => releases.find((r) => r.id === id)?.version ?? '—'
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Rollouts</CardTitle>
          <CardDescription>Atualização seletiva com fila e limite de concorrência; rollback gera um novo rollout inverso.</CardDescription>
        </div>
        {manage ? (
          <Button onClick={() => setCreating(true)}>
            <Plus aria-hidden="true" />
            Novo rollout
          </Button>
        ) : null}
      </CardHeader>
      <Loadable q={q}>
        {(d) =>
          d.items.length ? (
            <div className="overflow-x-auto">
              <Table>
                <caption className="sr-only">Rollouts</caption>
                <TableHeader>
                  <TableRow>
                    <TableHead scope="col">Versão</TableHead>
                    <TableHead scope="col">Alvo</TableHead>
                    <TableHead scope="col">Execução</TableHead>
                    <TableHead scope="col">Situação</TableHead>
                    <TableHead scope="col">Criado</TableHead>
                    <TableHead scope="col" className="text-right">
                      Ações
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {d.items.map((r) => (
                    <TableRow key={r.id}>
                      <TableCell className="font-semibold tabular-nums">
                        {versionOf(r.release_id)}
                        {r.is_rollback ? (
                          <Badge tone="warning" className="ml-2">
                            Rollback
                          </Badge>
                        ) : null}
                      </TableCell>
                      <TableCell>
                        {labelOf(CP_TARGET_KIND, r.target_kind)}
                        {r.target_kind === 'UMA' ? ` · ${instances.find((i) => i.id === r.target_ref)?.name ?? ''}` : ''}
                        {r.target_kind === 'GRUPO' ? ` · ${groups.find((g) => g.id === r.target_ref)?.name ?? ''}` : ''}
                        <span className="block text-xs text-text-muted">até {r.max_concurrency} por vez</span>
                      </TableCell>
                      <TableCell>
                        {r.simulated ? (
                          <Badge tone="info">
                            <FlaskConical aria-hidden="true" />
                            SIMULAÇÃO
                          </Badge>
                        ) : (
                          <Badge tone="neutral">Executor externo (não configurado)</Badge>
                        )}
                      </TableCell>
                      <TableCell>
                        <ToneBadge value={lookup(CP_ROLLOUT_STATUS, r.status)} />
                      </TableCell>
                      <TableCell className="tabular-nums">{formatDateTime(r.created_at)}</TableCell>
                      <TableCell className="text-right">
                        <Button asChild variant="ghost">
                          <Link to={`/control-plane/rollouts/${r.id}`}>Abrir</Link>
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          ) : (
            <EmptyState size="compact" headingLevel="h3" title="Nenhum rollout." description="Publique uma release e crie um rollout para atualizar instâncias." />
          )
        }
      </Loadable>
      <RolloutCreateDialog open={creating} onOpenChange={setCreating} releases={releases} instances={instances} groups={groups} />
    </Card>
  )
}

function RolloutCreateDialog({
  open,
  onOpenChange,
  releases,
  instances,
  groups,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  releases: CpRelease[]
  instances: CpInstance[]
  groups: CpGroup[]
}) {
  const qc = useQueryClient()
  const eligible = releases.filter((r) => r.status === 'PUBLICADA' || r.status === 'VALIDADA')
  const [releaseId, setReleaseId] = React.useState('')
  const [kind, setKind] = React.useState<'UMA' | 'GRUPO' | 'TODAS'>('UMA')
  const [ref, setRef] = React.useState('')
  const [maxConc, setMaxConc] = React.useState('1')
  const [policy, setPolicy] = React.useState<'PARAR' | 'CONTINUAR'>('PARAR')
  const [executor, setExecutor] = React.useState<'NULO' | 'SIMULACAO_LOCAL'>('NULO')
  const [reason, setReason] = React.useState('')
  const [start, setStart] = React.useState(true)
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)
  const idem = React.useRef(newRequestId())
  React.useEffect(() => {
    if (!open) {
      setReleaseId('')
      setKind('UMA')
      setRef('')
      setMaxConc('1')
      setPolicy('PARAR')
      setExecutor('NULO')
      setReason('')
      setStart(true)
      setError(null)
      idem.current = newRequestId()
    }
  }, [open])
  const conc = Number(maxConc)
  const ok = !!releaseId && (kind === 'TODAS' || !!ref) && Number.isInteger(conc) && conc >= 1 && conc <= 20
  return (
    <Dialog open={open} onOpenChange={(o) => (!pending ? onOpenChange(o) : undefined)}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>Novo rollout</DialogTitle>
          <DialogDescription>Escolha a versão, o alvo e quantas instâncias atualizar ao mesmo tempo.</DialogDescription>
        </DialogHeader>
        <form
          noValidate
          className="flex flex-col gap-5"
          onSubmit={async (e) => {
            e.preventDefault()
            if (!ok) return
            setPending(true)
            setError(null)
            try {
              await http.post(
                '/control-plane/rollouts',
                {
                  release_id: releaseId,
                  target_kind: kind,
                  target_ref: kind === 'TODAS' ? null : ref,
                  max_concurrency: conc,
                  failure_policy: policy,
                  executor,
                  reason: reason.trim() || null,
                  start,
                },
                { idempotencyKey: idem.current },
              )
              void qc.invalidateQueries({ queryKey: ['control-plane'] })
              toast.success(executor === 'SIMULACAO_LOCAL' ? 'Rollout criado (SIMULAÇÃO).' : 'Rollout criado.')
              onOpenChange(false)
            } catch (err) {
              setError(errorMessage(err))
            } finally {
              setPending(false)
            }
          }}
        >
          <FieldGroup>
            <Field required>
              <FieldLabel>Versão</FieldLabel>
              <Select value={releaseId} onValueChange={setReleaseId}>
                <SelectTrigger>
                  <SelectValue placeholder={eligible.length ? 'Selecione' : 'Nenhuma versão validada/publicada'} />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    {eligible.map((r) => (
                      <SelectItem key={r.id} value={r.id}>
                        {r.version} — {lookup(CP_RELEASE_STATUS, r.status).label}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
            </Field>
            <Field>
              <FieldLabel id="ro-kind">Alvo</FieldLabel>
              <ToggleGroup
                type="single"
                aria-labelledby="ro-kind"
                value={kind}
                onValueChange={(v) => {
                  if (v) {
                    setKind(v as 'UMA' | 'GRUPO' | 'TODAS')
                    setRef('')
                  }
                }}
                className="w-fit"
              >
                <ToggleGroupItem value="UMA">Uma instância</ToggleGroupItem>
                <ToggleGroupItem value="GRUPO">Grupo</ToggleGroupItem>
                <ToggleGroupItem value="TODAS">Todas</ToggleGroupItem>
              </ToggleGroup>
            </Field>
            {kind !== 'TODAS' ? (
              <Field required>
                <FieldLabel>{kind === 'UMA' ? 'Instância' : 'Grupo'}</FieldLabel>
                <Select value={ref} onValueChange={setRef}>
                  <SelectTrigger>
                    <SelectValue placeholder="Selecione" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      {(kind === 'UMA' ? instances.map((i) => ({ id: i.id, label: `${i.name} (${i.current_version ?? 'sem heartbeat'})` })) : groups.map((g) => ({ id: g.id, label: g.name }))).map(
                        (o) => (
                          <SelectItem key={o.id} value={o.id}>
                            {o.label}
                          </SelectItem>
                        ),
                      )}
                    </SelectGroup>
                  </SelectContent>
                </Select>
              </Field>
            ) : null}
            <Field>
              <FieldLabel>Máximo simultâneo</FieldLabel>
              <Input type="number" min={1} max={20} value={maxConc} onChange={(e) => setMaxConc(e.target.value)} className="w-28" />
              <FieldDescription>De 1 a 20 instâncias atualizando ao mesmo tempo (evita atualizar tudo de uma vez).</FieldDescription>
            </Field>
            <Field>
              <FieldLabel id="ro-policy">Em caso de falha</FieldLabel>
              <ToggleGroup type="single" aria-labelledby="ro-policy" value={policy} onValueChange={(v) => v && setPolicy(v as 'PARAR' | 'CONTINUAR')} className="w-fit">
                <ToggleGroupItem value="PARAR">Parar o rollout</ToggleGroupItem>
                <ToggleGroupItem value="CONTINUAR">Continuar com as demais</ToggleGroupItem>
              </ToggleGroup>
            </Field>
            <Field>
              <FieldLabel id="ro-exec">Execução</FieldLabel>
              <ToggleGroup type="single" aria-labelledby="ro-exec" value={executor} onValueChange={(v) => v && setExecutor(v as 'NULO' | 'SIMULACAO_LOCAL')} className="w-fit">
                <ToggleGroupItem value="NULO">Executor real (dependência externa)</ToggleGroupItem>
                <ToggleGroupItem value="SIMULACAO_LOCAL">SIMULAÇÃO local</ToggleGroupItem>
              </ToggleGroup>
              <FieldDescription>
                {executor === 'NULO'
                  ? 'Sem o Portainer configurado, os alvos ficam “Bloqueado — dependência externa”. Nada é implantado.'
                  : 'Simulação rotulada: avança a fila sem implantar nada e nunca muda a versão reportada pelas instâncias.'}
              </FieldDescription>
            </Field>
            <Field>
              <FieldLabel>Motivo</FieldLabel>
              <Input value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} />
            </Field>
            <Field orientation="horizontal" className="min-h-11">
              <Checkbox checked={start} onCheckedChange={(c) => setStart(c === true)} />
              <FieldLabel className="font-normal">Iniciar imediatamente</FieldLabel>
            </Field>
          </FieldGroup>
          {error ? (
            <Alert variant="danger">
              <AlertDescription className="text-text">{error}</AlertDescription>
            </Alert>
          ) : null}
          <DialogFooter>
            <Button variant="secondary" onClick={() => onOpenChange(false)} disabled={pending}>
              Cancelar
            </Button>
            <Button type="submit" loading={pending} disabled={!ok}>
              Criar rollout
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
