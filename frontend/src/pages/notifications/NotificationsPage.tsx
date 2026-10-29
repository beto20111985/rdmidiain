/**
 * Notificações — central (GET /notifications, marcar lida / todas), preferências por tipo
 * e canal (GET/PUT /notifications/preferences; push aparece "indisponível" quando o
 * servidor diz), modelos de mensagem (admin: editar + pré-visualizar) e comunicados.
 * O contador do sino é o real (GET /notifications/unread-count): marcar como lida invalida
 * o domínio `notifications`, e o evento WS `notification.created` também.
 */
import * as React from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Bell, CheckCheck, Eye, Megaphone, RotateCcw, Save } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Checkbox } from '@/components/ui/checkbox'
import { Switch } from '@/components/ui/switch'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Field, FieldDescription, FieldGroup, FieldLabel, FieldLegend, FieldSet } from '@/components/ui/field'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { EmptyState, ErrorState, PageSkeleton } from '@/components/patterns/states'
import { useCan } from '@/hooks/usePermissions'
import { useTableState } from '@/hooks/useTableState'
import { useUnreadNotifications } from '@/hooks/useUnreadNotifications'
import { http, type Paginated } from '@/lib/http'
import { qk } from '@/lib/queryKeys'
import { errorMessage, isApiError } from '@/lib/errors'
import { formatDateTime } from '@/lib/format'
import { CHANNEL_LABELS, DELIVERY_STATUS, labelOf, lookup } from '@/components/admin/labels'
import { RealtimeStale } from '@/components/admin/ui'
import { OrgPicker, type PickedEntity } from '@/components/admin/pickers'
import { PERMISSIONS } from '@/lib/permissions'
import type { NotificationItem, PreferenceItem, TemplateItem } from './types'

type Tab = 'central' | 'preferencias' | 'modelos' | 'comunicados'

export default function NotificationsPage() {
  const can = useCan()
  const [sp, setSp] = useSearchParams()
  const manage = can('notifications.manage')
  const raw = sp.get('aba') as Tab | null
  const tab: Tab = raw === 'preferencias' || ((raw === 'modelos' || raw === 'comunicados') && manage) ? raw : 'central'
  const unread = useUnreadNotifications()
  return (
    <>
      <PageHeader
        title="Notificações"
        description={unread.isSuccess ? (unread.data.count ? `${unread.data.count} não lida(s)` : 'Nenhuma não lida') : undefined}
      />
      <Tabs
        value={tab}
        onValueChange={(v) =>
          setSp(
            (prev) => {
              const n = new URLSearchParams(prev)
              if (v === 'central') n.delete('aba')
              else n.set('aba', v)
              n.delete('page')
              return n
            },
            { replace: true },
          )
        }
      >
        <TabsList aria-label="Seções de notificações">
          <TabsTrigger value="central">Central</TabsTrigger>
          <TabsTrigger value="preferencias">Preferências</TabsTrigger>
          {manage ? <TabsTrigger value="modelos">Modelos de mensagem</TabsTrigger> : null}
          {manage ? <TabsTrigger value="comunicados">Comunicados</TabsTrigger> : null}
        </TabsList>
        <TabsContent value="central">
          <CentralTab />
        </TabsContent>
        <TabsContent value="preferencias">
          <PreferencesTab />
        </TabsContent>
        {manage ? (
          <TabsContent value="modelos">
            <TemplatesTab />
          </TabsContent>
        ) : null}
        {manage ? (
          <TabsContent value="comunicados">
            <AnnouncementsTab />
          </TabsContent>
        ) : null}
      </Tabs>
    </>
  )
}

function CentralTab() {
  const qc = useQueryClient()
  const table = useTableState({ defaultPageSize: 25, filterKeys: ['nao_lidas'] })
  const unreadOnly = table.state.filters.nao_lidas === '1'
  const params = { page: table.state.page, page_size: table.state.pageSize, unread_only: unreadOnly || undefined }
  const q = useQuery({
    queryKey: qk.list('notifications', params),
    queryFn: () => http.get<Paginated<NotificationItem>>('/notifications', { query: params }),
    placeholderData: keepPreviousData,
  })
  const markOne = useMutation({
    mutationFn: (id: string) => http.post(`/notifications/${id}/read`),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['notifications'] }),
    onError: (e) => toast.error(errorMessage(e)),
  })
  const markAll = useMutation({
    mutationFn: () => http.post<{ updated: number }>('/notifications/read-all'),
    onSuccess: (r) => {
      toast.success(r.updated ? `${r.updated} notificação(ões) marcada(s) como lida(s).` : 'Nada para marcar.')
      void qc.invalidateQueries({ queryKey: ['notifications'] })
    },
    onError: (e) => toast.error(errorMessage(e)),
  })

  const columns: DataTableColumn<NotificationItem>[] = [
    {
      id: 'title',
      header: 'Notificação',
      mobile: 'primary',
      cell: (n) => (
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="flex flex-wrap items-center gap-2">
            {!n.read ? <span className="sr-only">Não lida: </span> : null}
            {n.link && n.link.startsWith('/') ? (
              <Link to={n.link} className={n.read ? 'text-text underline-offset-4 hover:underline' : 'font-bold text-link underline-offset-4 hover:underline'} onClick={() => !n.read && markOne.mutate(n.id)}>
                {n.title}
              </Link>
            ) : (
              <span className={n.read ? 'text-text' : 'font-bold text-text'}>{n.title}</span>
            )}
            {!n.read ? <Badge tone="info">Nova</Badge> : null}
          </span>
          <span className="whitespace-pre-line text-sm text-text-muted">{n.body}</span>
        </div>
      ),
    },
    { id: 'at', header: 'Recebida', mobile: 'secondary', cell: (n) => <span className="tabular-nums">{formatDateTime(n.created_at)}</span> },
    {
      id: 'channels',
      header: 'Canais',
      mobile: 'field',
      cell: (n) =>
        n.deliveries.length ? (
          <ul className="m-0 flex list-none flex-col gap-0.5 p-0 text-xs">
            {n.deliveries.map((d) => (
              <li key={d.channel} title={d.detail ?? undefined}>
                {labelOf(CHANNEL_LABELS, d.channel)}: <span className="font-semibold">{lookup(DELIVERY_STATUS, d.status).label}</span>
              </li>
            ))}
          </ul>
        ) : (
          'Central'
        ),
    },
  ]

  return (
    <div className="flex flex-col gap-4">
      <RealtimeStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />
      <Card>
        <DataTable
          caption="Notificações"
          columns={columns}
          data={q.data}
          getRowId={(n) => n.id}
          controller={table}
          isLoading={q.isLoading}
          isFetching={q.isFetching}
          error={q.error}
          onRetry={() => void q.refetch()}
          filtersActive={unreadOnly}
          empty={{ icon: Bell, title: 'Nenhuma notificação por aqui.', description: 'Avisos de pagamento, campanhas, telas e chamados aparecem nesta central.' }}
          rowActions={(n) =>
            !n.read ? (
              <Button variant="ghost" onClick={() => markOne.mutate(n.id)} disabled={markOne.isPending && markOne.variables === n.id} aria-label={`Marcar “${n.title}” como lida`}>
                <Eye aria-hidden="true" />
                Marcar como lida
              </Button>
            ) : (
              <span className="text-xs text-text-muted">Lida {formatDateTime(n.read_at)}</span>
            )
          }
          toolbar={
            <>
              <ToggleGroup
                type="single"
                aria-label="Filtrar notificações"
                value={unreadOnly ? 'nao_lidas' : 'todas'}
                onValueChange={(v) => v && table.setFilter('nao_lidas', v === 'nao_lidas' ? '1' : null)}
              >
                <ToggleGroupItem value="todas">Todas</ToggleGroupItem>
                <ToggleGroupItem value="nao_lidas">Não lidas</ToggleGroupItem>
              </ToggleGroup>
              <Button variant="secondary" className="ml-auto" onClick={() => markAll.mutate()} loading={markAll.isPending}>
                <CheckCheck aria-hidden="true" />
                Marcar todas como lidas
              </Button>
            </>
          }
        />
      </Card>
    </div>
  )
}

function PreferencesTab() {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['notifications', 'preferences'], queryFn: () => http.get<{ items: PreferenceItem[] }>('/notifications/preferences').then((r) => r.items) })
  const [changes, setChanges] = React.useState<Record<string, boolean>>({})
  const save = useMutation({
    mutationFn: () =>
      http.put<{ items: PreferenceItem[] }>('/notifications/preferences', {
        items: Object.entries(changes).map(([k, enabled]) => {
          const [event_type, channel] = k.split('|')
          return { event_type, channel, enabled }
        }),
      }),
    onSuccess: (r) => {
      qc.setQueryData(['notifications', 'preferences'], r.items)
      setChanges({})
      toast.success('Preferências salvas.')
    },
  })
  if (q.isLoading) return <PageSkeleton />
  if (q.error || !q.data) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />
  const push = q.data.flatMap((i) => i.channels).find((c) => c.channel === 'PUSH')
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Como você quer ser avisado</CardTitle>
          <CardDescription>Notificações obrigatórias continuam na central mesmo se você desativar os outros canais.</CardDescription>
        </div>
      </CardHeader>
      {push && !push.available ? (
        <Alert variant="neutral">
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <AlertTitle>Notificações push indisponíveis</AlertTitle>
            <AlertDescription>{push.unavailable_reason ?? 'O provedor de push ainda não está configurado.'}</AlertDescription>
          </div>
        </Alert>
      ) : null}
      <div className="overflow-x-auto">
        <Table>
          <caption className="sr-only">Preferências por tipo de notificação e canal</caption>
          <TableHeader>
            <TableRow>
              <TableHead scope="col">Tipo</TableHead>
              {(['IN_APP', 'EMAIL', 'PUSH'] as const).map((c) => (
                <TableHead key={c} scope="col" className="text-center">
                  {CHANNEL_LABELS[c]}
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {q.data.map((item) => (
              <TableRow key={item.event_type}>
                <TableCell>
                  <span className="font-semibold">{item.label}</span>
                  {item.critical ? (
                    <Badge tone="warning" className="ml-2">
                      Obrigatória
                    </Badge>
                  ) : null}
                </TableCell>
                {item.channels.map((c) => {
                  const key = `${item.event_type}|${c.channel}`
                  const checked = key in changes ? changes[key] : c.enabled
                  const disabled = c.locked || !c.available
                  const label = `${item.label} — ${CHANNEL_LABELS[c.channel] ?? c.channel}${c.locked ? ' (obrigatória)' : !c.available ? ' (indisponível)' : ''}`
                  return (
                    <TableCell key={c.channel} className="text-center">
                      <div className="inline-flex min-h-11 items-center justify-center">
                        <Switch
                          aria-label={label}
                          checked={c.available ? checked : false}
                          disabled={disabled}
                          onCheckedChange={(v) =>
                            setChanges((prev) => {
                              const n = { ...prev }
                              if (v === c.enabled) delete n[key]
                              else n[key] = v
                              return n
                            })
                          }
                        />
                      </div>
                      {!c.available ? <span className="block text-xs text-text-muted">Indisponível</span> : null}
                    </TableCell>
                  )
                })}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      {save.error ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">{errorMessage(save.error)}</AlertDescription>
        </Alert>
      ) : null}
      <div className="flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
        <Button variant="secondary" onClick={() => setChanges({})} disabled={!Object.keys(changes).length || save.isPending}>
          Descartar
        </Button>
        <Button onClick={() => save.mutate()} loading={save.isPending} disabled={!Object.keys(changes).length}>
          <Save aria-hidden="true" />
          Salvar preferências
        </Button>
      </div>
    </Card>
  )
}

function TemplatesTab() {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['notifications', 'templates'], queryFn: () => http.get<{ items: TemplateItem[] }>('/notifications/templates').then((r) => r.items) })
  const [selected, setSelected] = React.useState<string>('')
  const [title, setTitle] = React.useState('')
  const [body, setBody] = React.useState('')
  const [active, setActive] = React.useState(true)
  const [preview, setPreview] = React.useState<{ title: string; body: string } | null>(null)
  const [busy, setBusy] = React.useState<'save' | 'preview' | 'reset' | null>(null)
  const [error, setError] = React.useState<string | null>(null)
  const current = q.data?.find((t) => `${t.event_type}|${t.channel}` === selected) ?? null
  React.useEffect(() => {
    if (current) {
      setTitle(current.title_template)
      setBody(current.body_template)
      setActive(current.is_active)
      setPreview(null)
      setError(null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected, current?.version])

  if (q.isLoading) return <PageSkeleton />
  if (q.error || !q.data) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />
  const [eventType, channel] = selected.split('|')
  const run = async (kind: 'save' | 'preview' | 'reset') => {
    if (!current) return
    setBusy(kind)
    setError(null)
    try {
      const base = `/notifications/templates/${encodeURIComponent(eventType)}/${channel}`
      if (kind === 'preview') setPreview(await http.post<{ title: string; body: string }>(`${base}/preview`, { title_template: title, body_template: body, data: {} }))
      else if (kind === 'save') {
        await http.put(base, { title_template: title, body_template: body, is_active: active, expected_version: current.version })
        await qc.invalidateQueries({ queryKey: ['notifications', 'templates'] })
        toast.success('Modelo salvo.')
      } else {
        await http.delete(base)
        await qc.invalidateQueries({ queryKey: ['notifications', 'templates'] })
        toast.success('Modelo restaurado para o padrão.')
      }
    } catch (e) {
      setError(isApiError(e) && e.code === 'TEMPLATE_VERSION_CONFLICT' ? 'O modelo foi alterado por outra pessoa. Recarregamos a versão atual.' : errorMessage(e))
      if (isApiError(e) && e.code === 'TEMPLATE_VERSION_CONFLICT') void qc.invalidateQueries({ queryKey: ['notifications', 'templates'] })
    } finally {
      setBusy(null)
    }
  }
  return (
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-12 xl:gap-6">
      <Card className="xl:col-span-7">
        <CardHeader>
          <div className="flex flex-col gap-1">
            <CardTitle>Modelo de mensagem</CardTitle>
            <CardDescription>Use somente as variáveis listadas, no formato {'{{nome}}'}. Modelos nunca executam código.</CardDescription>
          </div>
        </CardHeader>
        <Field>
          <FieldLabel>Tipo e canal</FieldLabel>
          <Select value={selected} onValueChange={setSelected}>
            <SelectTrigger>
              <SelectValue placeholder="Selecione o tipo de notificação e o canal" />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                {q.data.map((t) => (
                  <SelectItem key={`${t.event_type}|${t.channel}`} value={`${t.event_type}|${t.channel}`}>
                    {t.event_type} · {labelOf(CHANNEL_LABELS, t.channel)}
                    {t.custom ? ' (personalizado)' : ''}
                  </SelectItem>
                ))}
              </SelectGroup>
            </SelectContent>
          </Select>
        </Field>
        {current ? (
          <form
            noValidate
            className="flex flex-col gap-5"
            onSubmit={(e) => {
              e.preventDefault()
              void run('save')
            }}
          >
            <p className="m-0 flex flex-wrap gap-1 text-xs text-text-muted">
              Variáveis:{' '}
              {current.variables.length ? current.variables.map((v) => <code key={v} className="rounded bg-field px-1">{`{{${v}}}`}</code>) : 'nenhuma'}
            </p>
            <FieldGroup>
              <Field required>
                <FieldLabel>Título</FieldLabel>
                <Input value={title} maxLength={255} onChange={(e) => setTitle(e.target.value)} />
              </Field>
              <Field required>
                <FieldLabel>Mensagem</FieldLabel>
                <Textarea value={body} maxLength={4000} onChange={(e) => setBody(e.target.value)} className="min-h-32" />
              </Field>
              <Field orientation="horizontal" className="min-h-11">
                <Switch checked={active} onCheckedChange={setActive} />
                <FieldLabel className="font-normal">Modelo ativo (desativado = usa o texto padrão)</FieldLabel>
              </Field>
            </FieldGroup>
            {error ? (
              <Alert variant="danger">
                <AlertDescription className="text-text">{error}</AlertDescription>
              </Alert>
            ) : null}
            <div className="flex flex-wrap justify-end gap-2">
              {current.custom ? (
                <Button variant="tertiary" onClick={() => void run('reset')} loading={busy === 'reset'}>
                  <RotateCcw aria-hidden="true" />
                  Restaurar padrão
                </Button>
              ) : null}
              <Button variant="secondary" onClick={() => void run('preview')} loading={busy === 'preview'}>
                <Eye aria-hidden="true" />
                Pré-visualizar
              </Button>
              <Button type="submit" loading={busy === 'save'} disabled={!title.trim() || !body.trim()}>
                <Save aria-hidden="true" />
                Salvar modelo
              </Button>
            </div>
          </form>
        ) : (
          <EmptyState size="compact" headingLevel="h3" title="Escolha um modelo para editar." />
        )}
      </Card>
      <Card className="xl:col-span-5">
        <CardHeader>
          <div className="flex flex-col gap-1">
            <CardTitle>Pré-visualização</CardTitle>
            <CardDescription>Gerada pelo servidor com valores de exemplo (&lt;variável&gt;).</CardDescription>
          </div>
        </CardHeader>
        {preview ? (
          <div className="flex flex-col gap-2 rounded-lg border border-border bg-field p-4">
            <p className="m-0 font-semibold text-text">{preview.title}</p>
            <p className="m-0 whitespace-pre-line text-sm text-text">{preview.body}</p>
          </div>
        ) : (
          <p className="text-sm text-text-muted">Clique em “Pré-visualizar” para ver o texto final.</p>
        )}
      </Card>
    </div>
  )
}

const ANNOUNCE_PERMS = PERMISSIONS.filter((p) => p.endsWith('.view') || p === 'campaigns.manage' || p === 'support.create')

function AnnouncementsTab() {
  const [title, setTitle] = React.useState('')
  const [message, setMessage] = React.useState('')
  const [audience, setAudience] = React.useState<'staff' | 'org'>('staff')
  const [org, setOrg] = React.useState<PickedEntity | null>(null)
  const [permission, setPermission] = React.useState('dashboard.view')
  const [channels, setChannels] = React.useState<string[]>(['IN_APP'])
  const [result, setResult] = React.useState<{ recipients: number; created: number; deduped: number; skipped_by_preference: number; push_unavailable: number } | null>(null)
  const m = useMutation({
    mutationFn: () =>
      http.post<{ recipients: number; created: number; deduped: number; skipped_by_preference: number; push_unavailable: number }>('/notifications/announcements', {
        title: title.trim(),
        message: message.trim(),
        org_id: audience === 'org' ? org?.id : null,
        permission,
        channels,
      }),
    onSuccess: (r) => {
      setResult(r)
      toast.success('Comunicado enviado.')
    },
  })
  const ok = title.trim().length >= 3 && message.trim().length >= 3 && channels.length > 0 && (audience === 'staff' || !!org)
  return (
    <Card className="max-w-3xl">
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Novo comunicado</CardTitle>
          <CardDescription>Aviso administrativo para a equipe RD ou para os usuários de uma empresa, filtrado por permissão.</CardDescription>
        </div>
      </CardHeader>
      <form
        noValidate
        className="flex flex-col gap-5"
        onSubmit={(e) => {
          e.preventDefault()
          if (ok) m.mutate()
        }}
      >
        <FieldGroup>
          <Field required>
            <FieldLabel>Título</FieldLabel>
            <Input value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} />
          </Field>
          <Field required>
            <FieldLabel>Mensagem</FieldLabel>
            <Textarea value={message} maxLength={2000} onChange={(e) => setMessage(e.target.value)} />
          </Field>
          <Field>
            <FieldLabel id="ann-aud">Destinatários</FieldLabel>
            <ToggleGroup type="single" aria-labelledby="ann-aud" value={audience} onValueChange={(v) => v && setAudience(v as 'staff' | 'org')} className="w-fit">
              <ToggleGroupItem value="staff">Equipe RD</ToggleGroupItem>
              <ToggleGroupItem value="org">Usuários de uma empresa</ToggleGroupItem>
            </ToggleGroup>
          </Field>
          {audience === 'org' ? <OrgPicker label="Empresa" value={org} onChange={setOrg} required /> : null}
          <Field>
            <FieldLabel>Somente quem tem a permissão</FieldLabel>
            <Select value={permission} onValueChange={setPermission}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  {ANNOUNCE_PERMS.map((p) => (
                    <SelectItem key={p} value={p}>
                      {p}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
            <FieldDescription>dashboard.view = todos os usuários com acesso ao painel.</FieldDescription>
          </Field>
          <FieldSet>
            <FieldLegend>Canais</FieldLegend>
            {(['IN_APP', 'EMAIL', 'PUSH'] as const).map((c) => (
              <Field key={c} orientation="horizontal" className="min-h-11">
                <Checkbox
                  checked={channels.includes(c)}
                  onCheckedChange={(v) => setChannels((prev) => (v === true ? [...prev, c] : prev.filter((x) => x !== c)))}
                />
                <FieldLabel className="font-normal">
                  {CHANNEL_LABELS[c]}
                  {c === 'PUSH' ? <span className="ml-1 text-xs text-text-muted">(registrado como indisponível enquanto não houver provedor)</span> : null}
                </FieldLabel>
              </Field>
            ))}
          </FieldSet>
        </FieldGroup>
        {m.error ? (
          <Alert variant="danger">
            <AlertDescription className="text-text">{errorMessage(m.error)}</AlertDescription>
          </Alert>
        ) : null}
        {result ? (
          <Alert variant="success">
            <div className="flex min-w-0 flex-1 flex-col gap-1">
              <AlertTitle>Comunicado enviado</AlertTitle>
              <AlertDescription>
                {result.recipients} destinatário(s) · {result.created} notificação(ões) criada(s) · {result.deduped} repetida(s) ignorada(s) ·{' '}
                {result.skipped_by_preference} desativada(s) por preferência · push indisponível: {result.push_unavailable}
              </AlertDescription>
            </div>
          </Alert>
        ) : null}
        <div className="flex justify-end">
          <Button type="submit" loading={m.isPending} disabled={!ok}>
            <Megaphone aria-hidden="true" />
            Enviar comunicado
          </Button>
        </div>
      </form>
    </Card>
  )
}
