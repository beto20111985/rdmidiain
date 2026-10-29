/**
 * Auditoria (`audit.view` / `audit.export`) — GET /audit com filtros (usuário, ação,
 * entidade, período, impersonation), detalhe antes/depois (já mascarado pelo servidor),
 * exportação CSV (GET /audit/export) e verificação da cadeia de hash (GET /audit/verify).
 * Datas do filtro no fuso America/Sao_Paulo (UTC−3, sem horário de verão desde 2019).
 */
import * as React from 'react'
import { keepPreviousData, useQuery, type UseQueryResult } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Download, Eye, RefreshCw, ScrollText, ShieldCheck, ShieldX, UserRoundCog } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Checkbox } from '@/components/ui/checkbox'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Field, FieldLabel } from '@/components/ui/field'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Sheet, SheetBody, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { useTableState } from '@/hooks/useTableState'
import { useCan } from '@/hooks/usePermissions'
import { http, type Paginated } from '@/lib/http'
import { qk } from '@/lib/queryKeys'
import { errorMessage, isApiError } from '@/lib/errors'
import { formatDateTime, formatNumber } from '@/lib/format'
import { downloadFile } from '@/components/admin/binary'
import { useUserDirectory } from '@/components/admin/directory'
import { BeforeAfter, KeyValueList, SectionTitle } from '@/components/admin/ui'
import type { AuditRow, AuditVerify } from '../types'
import { nextDayStartUtc, startOfDayUtc } from '@/lib/dates'

const FILTERS = ['actor', 'action', 'entity_type', 'entity_id', 'from', 'to', 'imp'] as const

export default function AuditPage() {
  const can = useCan()
  const table = useTableState({ defaultPageSize: 25, filterKeys: [...FILTERS] })
  const f = table.state.filters
  const directory = useUserDirectory()
  const [detail, setDetail] = React.useState<AuditRow | null>(null)
  const [exporting, setExporting] = React.useState(false)

  const apiFilters = {
    actor_user_id: f.actor,
    action: f.action,
    entity_type: f.entity_type,
    entity_id: f.entity_id,
    date_from: f.from ? (startOfDayUtc(f.from) ?? undefined) : undefined,
    date_to: f.to ? (nextDayStartUtc(f.to) ?? undefined) : undefined,
    impersonated: f.imp === '1' ? true : undefined,
  }
  const params = { page: table.state.page, page_size: table.state.pageSize, ...apiFilters }
  const q = useQuery({
    queryKey: qk.list('audit', params),
    queryFn: () => http.get<Paginated<AuditRow>>('/audit', { query: params }),
    placeholderData: keepPreviousData,
  })
  const verify = useQuery({
    queryKey: qk.sub('audit', 'verify'),
    queryFn: () => http.get<AuditVerify>('/audit/verify'),
    retry: false,
    staleTime: Infinity,
  })

  // O servidor aplica `impersonated=true` (W1). Defesa: se algum dia ignorar o filtro, a tela
  // filtra a página atual e AVISA (nunca esconde o fato).
  const serverIgnoredImp = f.imp === '1' && !!q.data?.items.some((r) => !r.real_user_id)
  const data = React.useMemo(() => {
    if (!q.data || !serverIgnoredImp) return q.data
    return { ...q.data, items: q.data.items.filter((r) => r.real_user_id) }
  }, [q.data, serverIgnoredImp])

  const who = (id: string | null) => (id ? directory.nameOf(id) ?? `${id.slice(0, 8)}…` : 'Sistema')

  const columns: DataTableColumn<AuditRow>[] = [
    { id: 'at', header: 'Data e hora', mobile: 'secondary', cell: (r) => <span className="tabular-nums">{formatDateTime(r.created_at)}</span> },
    {
      id: 'actor',
      header: 'Usuário',
      mobile: 'field',
      cell: (r) => (
        <div className="flex flex-col gap-0.5">
          <span>{r.actor_device_id && !r.actor_user_id ? 'Dispositivo (player)' : who(r.actor_user_id)}</span>
          {r.real_user_id ? (
            <Badge tone="warning" className="w-fit">
              <UserRoundCog aria-hidden="true" />
              Impersonation por {who(r.real_user_id)}
            </Badge>
          ) : null}
        </div>
      ),
    },
    { id: 'action', header: 'Ação', mobile: 'primary', cell: (r) => <code className="text-sm font-semibold text-text">{r.action}</code> },
    {
      id: 'entity',
      header: 'Entidade',
      mobile: 'field',
      cell: (r) => (r.entity_type ? <span className="break-all">{r.entity_type}{r.entity_id ? ` · ${r.entity_id}` : ''}</span> : '—'),
    },
    { id: 'origin', header: 'Origem', mobile: 'field', cell: (r) => r.origin ?? '—' },
    { id: 'ip', header: 'IP', mobile: 'hidden', cell: (r) => <span className="tabular-nums">{r.ip ?? '—'}</span> },
  ]

  const doExport = async () => {
    setExporting(true)
    try {
      await downloadFile('/audit/export', 'auditoria.csv', { query: apiFilters })
      toast.success('Exportação da auditoria gerada (CSV).')
    } catch (e) {
      toast.error(errorMessage(e))
    } finally {
      setExporting(false)
    }
  }

  return (
    <>
      <PageHeader
        title="Auditoria"
        description="Registro append-only de ações relevantes, com usuário real e impersonado."
        actions={
          can('audit.export') ? (
            <Button variant="secondary" onClick={() => void doExport()} loading={exporting} loadingText="Gerando CSV…">
              <Download aria-hidden="true" />
              Exportar CSV
            </Button>
          ) : undefined
        }
      />
      <div className="flex flex-col gap-4">
        <ChainStatus q={verify} />
        {serverIgnoredImp ? (
          <Alert variant="warning">
            <div className="flex min-w-0 flex-1 flex-col gap-1">
              <AlertTitle>Filtro de impersonation aplicado só nesta página</AlertTitle>
              <AlertDescription>
                O servidor ainda não filtra por impersonation; a lista mostra apenas os registros desta página feitos durante um acesso como outro
                usuário. A exportação traz todos os registros dos demais filtros.
              </AlertDescription>
            </div>
          </Alert>
        ) : null}
        <Card>
          <DataTable
            caption="Registros de auditoria"
            columns={columns}
            data={data}
            getRowId={(r) => r.id}
            controller={table}
            isLoading={q.isLoading}
            isFetching={q.isFetching}
            error={q.error}
            onRetry={() => void q.refetch()}
            filtersActive={Object.keys(f).length > 0}
            empty={{ icon: ScrollText, title: 'Nenhum registro de auditoria ainda.', description: 'As ações relevantes aparecem aqui assim que acontecem.' }}
            rowActions={(r) => (
              <Button variant="ghost" onClick={() => setDetail(r)} aria-label={`Ver detalhes de ${r.action} em ${formatDateTime(r.created_at)}`}>
                <Eye aria-hidden="true" />
                Detalhes
              </Button>
            )}
            toolbar={<AuditFilters table={table} users={directory.users} />}
          />
        </Card>
      </div>
      <Sheet open={!!detail} onOpenChange={(o) => !o && setDetail(null)}>
        <SheetContent className="max-w-2xl">
          <SheetHeader>
            <SheetTitle>{detail?.action}</SheetTitle>
            <SheetDescription>Registro nº {detail?.seq} · {formatDateTime(detail?.created_at)}</SheetDescription>
          </SheetHeader>
          {detail ? (
            <SheetBody className="flex flex-col gap-6 py-6">
              <KeyValueList
                items={[
                  { label: 'Usuário', value: detail.actor_user_id ? who(detail.actor_user_id) : detail.actor_device_id ? `Dispositivo ${detail.actor_device_id}` : 'Sistema' },
                  { label: 'Usuário real (impersonation)', value: detail.real_user_id ? who(detail.real_user_id) : 'Não' },
                  { label: 'Entidade', value: detail.entity_type ? `${detail.entity_type} · ${detail.entity_id ?? '—'}` : null },
                  { label: 'Origem', value: detail.origin },
                  { label: 'Motivo', value: detail.reason, wide: true },
                  { label: 'IP', value: detail.ip },
                  { label: 'ID da requisição', value: detail.request_id ? <code className="break-all text-xs">{detail.request_id}</code> : null },
                  { label: 'Navegador', value: detail.user_agent, wide: true },
                  { label: 'Hash do registro', value: detail.row_hash ? <code className="break-all text-xs">{detail.row_hash}</code> : null, wide: true },
                ]}
              />
              <section aria-labelledby="audit-diff" className="flex flex-col gap-3">
                <SectionTitle as="h3">
                  <span id="audit-diff">Antes e depois</span>
                </SectionTitle>
                <p className="text-xs text-text-muted">Dados sensíveis já chegam mascarados pelo servidor.</p>
                <BeforeAfter before={detail.before} after={detail.after} />
              </section>
            </SheetBody>
          ) : null}
        </SheetContent>
      </Sheet>
    </>
  )
}

function ChainStatus({ q }: { q: UseQueryResult<AuditVerify> }) {
  if (q.isLoading) return <p className="text-sm text-text-muted">Verificando a cadeia de integridade…</p>
  if (q.error) {
    // Sem escopo global a verificação não é exposta: não mostra nada inventado.
    if (isApiError(q.error) && (q.error.status === 403 || q.error.status === 404)) return null
    return (
      <Alert variant="warning">
        <AlertDescription>Não foi possível verificar a cadeia agora: {errorMessage(q.error)}</AlertDescription>
      </Alert>
    )
  }
  if (!q.data) return null
  const ok = q.data.ok
  return (
    <Alert variant={ok ? 'success' : 'danger'}>
      {ok ? <ShieldCheck aria-hidden="true" /> : <ShieldX aria-hidden="true" />}
      <div className="flex flex-1 flex-col gap-1">
        <AlertTitle>{ok ? 'Cadeia de auditoria íntegra' : 'Inconsistência detectada na cadeia de auditoria'}</AlertTitle>
        <AlertDescription>
          {ok
            ? `${formatNumber(q.data.rows_checked)} registro(s) verificados por encadeamento de hash.`
            : `Primeiro registro divergente: nº ${q.data.first_bad_seq ?? '—'}. ${q.data.problem ?? ''} Acione a equipe técnica.`}
        </AlertDescription>
      </div>
      <Button variant="tertiary" onClick={() => void q.refetch()} loading={q.isFetching}>
        <RefreshCw aria-hidden="true" />
        Verificar de novo
      </Button>
    </Alert>
  )
}

function AuditFilters({ table, users }: { table: ReturnType<typeof useTableState>; users: Array<{ id: string; name: string; restricted?: boolean }> }) {
  const f = table.state.filters
  const [action, setAction] = React.useState(f.action ?? '')
  const [entityType, setEntityType] = React.useState(f.entity_type ?? '')
  const [entityId, setEntityId] = React.useState(f.entity_id ?? '')
  React.useEffect(() => {
    setAction(f.action ?? '')
    setEntityType(f.entity_type ?? '')
    setEntityId(f.entity_id ?? '')
  }, [f.action, f.entity_type, f.entity_id])
  const apply = () => {
    table.setFilter('action', action.trim() || null)
    table.setFilter('entity_type', entityType.trim() || null)
    table.setFilter('entity_id', entityId.trim() || null)
  }
  return (
    <form
      className="flex w-full flex-wrap items-end gap-3"
      onSubmit={(e) => {
        e.preventDefault()
        apply()
      }}
    >
      {users.length ? (
        <Field className="w-full sm:w-56">
          <FieldLabel>Usuário</FieldLabel>
          <Select value={f.actor ?? 'all'} onValueChange={(v) => table.setFilter('actor', v === 'all' ? null : v)}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                <SelectItem value="all">Todos</SelectItem>
                {users.map((u) => (
                  <SelectItem key={u.id} value={u.id}>
                    {u.name}
                  </SelectItem>
                ))}
              </SelectGroup>
            </SelectContent>
          </Select>
        </Field>
      ) : null}
      <Field className="w-full sm:w-52">
        <FieldLabel>Ação</FieldLabel>
        <Input value={action} onChange={(e) => setAction(e.target.value)} placeholder="ex.: auth.* ou user.updated" spellCheck={false} />
      </Field>
      <Field className="w-full sm:w-44">
        <FieldLabel>Entidade</FieldLabel>
        <Input value={entityType} onChange={(e) => setEntityType(e.target.value)} placeholder="ex.: user" spellCheck={false} />
      </Field>
      <Field className="w-full sm:w-52">
        <FieldLabel>ID da entidade</FieldLabel>
        <Input value={entityId} onChange={(e) => setEntityId(e.target.value)} spellCheck={false} />
      </Field>
      <Field className="w-full sm:w-40">
        <FieldLabel>De</FieldLabel>
        <Input type="date" value={f.from ?? ''} onChange={(e) => table.setFilter('from', e.target.value || null)} />
      </Field>
      <Field className="w-full sm:w-40">
        <FieldLabel>Até</FieldLabel>
        <Input type="date" value={f.to ?? ''} onChange={(e) => table.setFilter('to', e.target.value || null)} />
      </Field>
      <Field orientation="horizontal" className="min-h-11 w-auto">
        <Checkbox checked={f.imp === '1'} onCheckedChange={(c) => table.setFilter('imp', c === true ? '1' : null)} />
        <FieldLabel className="font-normal">Somente durante impersonation</FieldLabel>
      </Field>
      <Button type="submit" variant="secondary">
        Aplicar filtros
      </Button>
    </form>
  )
}
