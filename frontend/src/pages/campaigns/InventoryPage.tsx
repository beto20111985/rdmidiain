/**
 * Inventário publicitário (PG-14; RD): disponibilidade/capacidade por região e período em
 * unidades de 30 s (capacidade = ciclo de rotação × telas vendáveis; ocupação = contratadas +
 * reservas válidas), políticas de rotação versionadas (GLOBAL/plano/estabelecimento/tela;
 * I e E múltiplos de 30 s, ciclo dividindo 1 hora), reservas de 2 h e relatório de
 * redistribuição por retirada definitiva de tela. Backend W-INV.
 */
import * as React from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Plus, Search } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Skeleton } from '@/components/ui/skeleton'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { ErrorState, NoPermission } from '@/components/patterns/states'
import { ToneBadge } from '@/components/campaigns/badges'
import { FilterSelect } from '@/components/campaigns/filter-select'
import { EntityPicker, type PickerOption } from '@/components/campaigns/entity-picker'
import { EstablishmentName } from '@/components/campaigns/org-name'
import { ReasonDialog } from '@/components/campaigns/reason-dialog'
import { useTableState } from '@/hooks/useTableState'
import { useCan } from '@/hooks/usePermissions'
import { qk } from '@/lib/queryKeys'
import { isApiError } from '@/lib/errors'
import { formatBps, formatDateTime, formatNumber } from '@/lib/format'
import { inventoryApi } from './lib/api'
import { HOLD_STATUS, UFS } from './lib/labels'
import { cityKey, validateRotation } from './lib/rules'
import { searchEstablishments, searchScreens } from './lib/pickers'
import { startOfDayUtc, timeZoneLabel } from '@/lib/dates'
import { formatPlainDate, shortId } from './lib/format'
import { formatRemaining, useNow } from './lib/hooks'
import { todayIso } from './lib/schedule'
import type { InventoryHold, ScreenRetirement } from './lib/types'
import { describeError } from '@/pages/campaigns/lib/errorText'

const TABS = [
  ['disponibilidade', 'Disponibilidade'],
  ['politicas', 'Políticas de rotação'],
  ['reservas', 'Reservas'],
  ['redistribuicao', 'Redistribuição'],
] as const

function addDays(iso: string, n: number): string {
  const d = new Date(`${iso}T12:00:00Z`)
  d.setUTCDate(d.getUTCDate() + n)
  return d.toISOString().slice(0, 10)
}

function Forbidden({ error, permission, onRetry }: { error: unknown; permission: string; onRetry: () => void }) {
  if (isApiError(error) && error.isForbidden) return <NoPermission permission={permission} />
  return <ErrorState error={error} onRetry={onRetry} size="compact" />
}

export default function InventoryPage() {
  const [sp, setSp] = useSearchParams()
  const tab = TABS.some(([k]) => k === sp.get('aba')) ? (sp.get('aba') as string) : 'disponibilidade'
  return (
    <>
      <PageHeader title="Inventário publicitário" description="Capacidade em unidades de 30 segundos por ciclo de rotação. Overbooking é recusado pelo servidor." />
      <Tabs
        value={tab}
        onValueChange={(v) =>
          setSp(
            (prev) => {
              const n = new URLSearchParams(prev)
              n.set('aba', v)
              return n
            },
            { replace: true },
          )
        }
      >
        <TabsList aria-label="Seções do inventário">
          {TABS.map(([k, l]) => (
            <TabsTrigger key={k} value={k}>
              {l}
            </TabsTrigger>
          ))}
        </TabsList>
        <Card className="mt-6">
          <TabsContent value="disponibilidade" className="mt-0">
            <AvailabilityTab />
          </TabsContent>
          <TabsContent value="politicas" className="mt-0">
            <PoliciesTab />
          </TabsContent>
          <TabsContent value="reservas" className="mt-0">
            <HoldsTab />
          </TabsContent>
          <TabsContent value="redistribuicao" className="mt-0">
            <RetirementsTab />
          </TabsContent>
        </Card>
      </Tabs>
    </>
  )
}

function AvailabilityTab() {
  const today = todayIso()
  const [uf, setUf] = React.useState('')
  const [city, setCity] = React.useState('')
  const [est, setEst] = React.useState<PickerOption | null>(null)
  const [from, setFrom] = React.useState(today)
  const [to, setTo] = React.useState(addDays(today, 13))
  const [params, setParams] = React.useState<Record<string, string> | null>(null)
  const [formError, setFormError] = React.useState<string | null>(null)
  const q = useQuery({
    queryKey: qk.sub('inventory', 'availability', params ?? {}),
    queryFn: () => inventoryApi.availability(params!),
    enabled: !!params,
    retry: false,
  })
  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!uf && !est) return setFormError('Informe a UF (e, opcionalmente, a cidade) ou um estabelecimento.')
    if (city && !uf) return setFormError('Para filtrar por cidade, escolha a UF.')
    if (!from || !to || to < from) return setFormError('Período inválido.')
    setFormError(null)
    const p: Record<string, string> = { date_from: from, date_to: to }
    if (est) p.establishment_id = est.id
    else if (city) p.city_key = cityKey(uf, city)
    else p.state = uf
    setParams(p)
  }
  const data = q.data
  return (
    <div className="flex flex-col gap-4">
      <form onSubmit={submit} className="grid grid-cols-1 gap-3 md:grid-cols-3 xl:grid-cols-6 xl:items-end" noValidate>
        <Field>
          <FieldLabel>UF</FieldLabel>
          <Select value={uf || '__none'} onValueChange={(v) => setUf(v === '__none' ? '' : v)}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                <SelectItem value="__none">—</SelectItem>
                {UFS.map((u) => (
                  <SelectItem key={u} value={u}>
                    {u}
                  </SelectItem>
                ))}
              </SelectGroup>
            </SelectContent>
          </Select>
        </Field>
        <Field>
          <FieldLabel>Cidade (opcional)</FieldLabel>
          <Input value={city} onChange={(e) => setCity(e.target.value)} placeholder="Ex.: Goiânia" />
        </Field>
        <div className="md:col-span-1 xl:col-span-2">
          <EntityPicker label="Ou estabelecimento" domain="establishments" value={est} onChange={setEst} search={searchEstablishments} />
        </div>
        <Field>
          <FieldLabel>De</FieldLabel>
          <Input type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
        </Field>
        <Field>
          <FieldLabel>Até</FieldLabel>
          <Input type="date" value={to} onChange={(e) => setTo(e.target.value)} />
          <FieldDescription>Até 31 dias e 500 telas por consulta.</FieldDescription>
        </Field>
        <div className="flex flex-col gap-1 xl:col-span-6">
          <Button type="submit" className="w-fit" loading={q.isFetching}>
            <Search aria-hidden="true" />
            Consultar
          </Button>
          <FieldError>{formError}</FieldError>
        </div>
      </form>
      {!params ? (
        <p className="m-0 text-sm text-text-muted">Escolha a região e o período para ver a capacidade.</p>
      ) : q.isLoading ? (
        <Skeleton className="h-48 w-full" />
      ) : q.error ? (
        <Forbidden error={q.error} permission="inventory.view" onRetry={() => void q.refetch()} />
      ) : data ? (
        <>
          <dl className="m-0 grid grid-cols-2 gap-4 md:grid-cols-3">
            <div>
              <dt className="text-xs text-text-muted">Telas na região</dt>
              <dd className="m-0 text-xl font-bold tabular-nums">{formatNumber(data.screens)}</dd>
            </div>
            <div>
              <dt className="text-xs text-text-muted">Telas vendáveis</dt>
              <dd className="m-0 text-xl font-bold tabular-nums">{formatNumber(data.sellable_screens)}</dd>
            </div>
            <div>
              <dt className="text-xs text-text-muted">Unidade</dt>
              <dd className="m-0 text-xl font-bold tabular-nums">{data.unit_seconds} s</dd>
            </div>
          </dl>
          <Table>
            <caption className="sr-only">Capacidade e ocupação por dia</caption>
            <TableHeader>
              <TableRow className="h-11 hover:bg-transparent">
                <TableHead scope="col">Dia</TableHead>
                <TableHead scope="col" numeric>
                  Capacidade (unid.)
                </TableHead>
                <TableHead scope="col" numeric>
                  Ocupadas
                </TableHead>
                <TableHead scope="col" numeric>
                  Livres
                </TableHead>
                <TableHead scope="col" numeric>
                  Ocupação
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.days.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={5} className="text-text-muted">
                    Nenhuma tela vendável nesta região/período.
                  </TableCell>
                </TableRow>
              ) : (
                data.days.map((d) => (
                  <TableRow key={d.date}>
                    <TableCell>{formatPlainDate(d.date)}</TableCell>
                    <TableCell numeric className="tabular-nums">
                      {formatNumber(d.capacity_units)}
                    </TableCell>
                    <TableCell numeric className="tabular-nums">
                      {formatNumber(d.used_units)}
                    </TableCell>
                    <TableCell numeric className="tabular-nums">
                      {formatNumber(d.free_units)}
                    </TableCell>
                    <TableCell numeric className="tabular-nums">
                      {formatBps(d.occupancy_bps, 1)}
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </>
      ) : null}
    </div>
  )
}

function PoliciesTab() {
  const can = useCan()
  const qc = useQueryClient()
  const table = useTableState({ defaultPageSize: 25, prefix: 'pol_', filterKeys: ['scope'] })
  const params = { page: table.state.page, page_size: table.state.pageSize, scope: table.state.filters.scope }
  const q = useQuery({ queryKey: qk.list('inventory', { policies: params }), queryFn: () => inventoryApi.policies(params), placeholderData: keepPreviousData, retry: false })
  const [creating, setCreating] = React.useState(false)
  if (q.error && isApiError(q.error) && q.error.isForbidden) return <NoPermission permission="settings.view" />
  const def = q.data?.default
  type Row = NonNullable<typeof q.data>['items'][number]
  const cols: DataTableColumn<Row>[] = [
    {
      id: 'scope',
      header: 'Escopo',
      mobile: 'primary',
      cell: (p) => (
        <span className="flex flex-col">
          <span className="font-semibold">{{ GLOBAL: 'Global', PARTNER_PLAN: 'Plano de parceiro', ESTABLISHMENT: 'Estabelecimento', SCREEN: 'Tela' }[p.scope]}</span>
          {p.scope === 'ESTABLISHMENT' ? <EstablishmentName id={p.scope_id} /> : p.scope_id ? <span className="text-xs text-text-muted">{shortId(p.scope_id)}</span> : null}
        </span>
      ),
    },
    { id: 'cycle', header: 'Ciclo', cell: (p) => `${p.internal_seconds} s interno + ${p.external_seconds} s externo (${p.cycle_seconds} s)` },
    { id: 'units', header: 'Unidades/hora', numeric: true, cell: (p) => `${formatNumber(p.units_per_hour)} (${p.cycles_per_hour} ciclos)` },
    { id: 'v120', header: 'Aceita 120 s', cell: (p) => (p.eligible_120s ? 'Sim' : 'Não') },
    { id: 'validity', header: 'Vigência', cell: (p) => `${formatDateTime(p.effective_from)} → ${p.effective_to ? formatDateTime(p.effective_to) : 'sem fim'}` },
    { id: 'reason', header: 'Motivo', cell: (p) => p.reason },
  ]
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-title-section">Políticas de rotação</h2>
        {can('settings.manage') ? (
          <Button onClick={() => setCreating(true)}>
            <Plus aria-hidden="true" />
            Nova versão de política
          </Button>
        ) : null}
      </div>
      {def ? (
        <p className="m-0 text-sm text-text-muted">
          Padrão vigente: {def.internal_seconds} s interno + {def.external_seconds} s externo · {formatNumber(def.units_per_hour ?? null)} unidades de 30 s por hora.
        </p>
      ) : null}
      <DataTable
        caption="Políticas de rotação"
        columns={cols}
        data={q.data}
        getRowId={(p) => p.id}
        controller={table}
        isLoading={q.isLoading}
        isFetching={q.isFetching}
        error={q.error}
        onRetry={() => void q.refetch()}
        filtersActive={!!table.state.filters.scope}
        empty={{ title: 'Somente a política padrão', description: 'Crie versões por plano, estabelecimento ou tela quando precisar de outro ciclo.' }}
        toolbar={
          <FilterSelect
            label="Escopo"
            value={table.state.filters.scope}
            onChange={(v) => table.setFilter('scope', v)}
            options={[
              { value: 'GLOBAL', label: 'Global' },
              { value: 'PARTNER_PLAN', label: 'Plano de parceiro' },
              { value: 'ESTABLISHMENT', label: 'Estabelecimento' },
              { value: 'SCREEN', label: 'Tela' },
            ]}
          />
        }
      />
      <PolicyDialog open={creating} onOpenChange={setCreating} onSaved={() => void qc.invalidateQueries({ queryKey: qk.root('inventory') })} />
    </div>
  )
}

function PolicyDialog({ open, onOpenChange, onSaved }: { open: boolean; onOpenChange: (o: boolean) => void; onSaved: () => void }) {
  const [scope, setScope] = React.useState<'GLOBAL' | 'PARTNER_PLAN' | 'ESTABLISHMENT' | 'SCREEN'>('ESTABLISHMENT')
  const [scopeRef, setScopeRef] = React.useState<PickerOption | null>(null)
  const [scopeId, setScopeId] = React.useState('')
  const [internal, setInternal] = React.useState('120')
  const [external, setExternal] = React.useState('120')
  const [from, setFrom] = React.useState('')
  const [reason, setReason] = React.useState('')
  const [errors, setErrors] = React.useState<Record<string, string>>({})
  const [error, setError] = React.useState<unknown>(null)
  const [pending, setPending] = React.useState(false)
  React.useEffect(() => {
    if (open) {
      setErrors({})
      setError(null)
      setFrom(addDays(todayIso(), 1))
    }
  }, [open])
  // estabelecimento/tela: a vigência começa à 00:00 no fuso dele; global/plano: fuso padrão de exibição
  const policyTz = scope === 'ESTABLISHMENT' || scope === 'SCREEN' ? (scopeRef?.timezone ?? undefined) : undefined
  const effectiveFrom = from ? startOfDayUtc(from, policyTz) : null
  const save = async () => {
    const e: Record<string, string> = {}
    const i = Number(internal)
    const x = Number(external)
    const rot = validateRotation(i, x)
    if (rot) e.rotation = rot
    if (scope === 'ESTABLISHMENT' && !scopeRef) e.scope = 'Escolha o estabelecimento.'
    if (scope === 'SCREEN' && !scopeRef) e.scope = 'Escolha a tela.'
    if (scope === 'PARTNER_PLAN' && !/^[0-9a-f-]{36}$/i.test(scopeId.trim())) e.scope = 'Informe o identificador (UUID).'
    if (from && !effectiveFrom) e.from = 'Data inválida.'
    if (!from) e.from = 'Informe o início da vigência.'
    if (reason.trim().length < 5) e.reason = 'Mínimo de 5 caracteres.'
    setErrors(e)
    if (Object.keys(e).length) return
    setPending(true)
    setError(null)
    try {
      await inventoryApi.createPolicy({
        scope,
        scope_id: scope === 'GLOBAL' ? null : scope === 'ESTABLISHMENT' || scope === 'SCREEN' ? scopeRef!.id : scopeId.trim(),
        internal_seconds: i,
        external_seconds: x,
        effective_from: effectiveFrom,
        reason: reason.trim(),
      })
      toast.success('Política criada.')
      onSaved()
      onOpenChange(false)
    } catch (err) {
      setError(err)
    } finally {
      setPending(false)
    }
  }
  const i = Number(internal)
  const x = Number(external)
  const preview = !validateRotation(i, x) ? `Ciclo de ${i + x} s · ${3600 / (i + x)} ciclos/h · ${(3600 / (i + x)) * (x / 30)} unidades de 30 s por hora` : null
  return (
    <Dialog open={open} onOpenChange={(o) => !pending && onOpenChange(o)}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>Nova versão de política de rotação</DialogTitle>
          <DialogDescription>
            Vigência não pode sobrepor outra do mesmo escopo; o servidor recusa mudança que reduza o já contratado. Vale por dia local de cada tela.
          </DialogDescription>
        </DialogHeader>
        <Field>
          <FieldLabel>Escopo</FieldLabel>
          <Select
            value={scope}
            onValueChange={(v) => {
              setScope(v as typeof scope)
              setScopeRef(null)
            }}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                <SelectItem value="GLOBAL">Global</SelectItem>
                <SelectItem value="PARTNER_PLAN">Plano de parceiro</SelectItem>
                <SelectItem value="ESTABLISHMENT">Estabelecimento</SelectItem>
                <SelectItem value="SCREEN">Tela</SelectItem>
              </SelectGroup>
            </SelectContent>
          </Select>
        </Field>
        {scope === 'ESTABLISHMENT' ? (
          <EntityPicker label="Estabelecimento" domain="establishments" value={scopeRef} onChange={setScopeRef} search={searchEstablishments} error={errors.scope} />
        ) : scope === 'SCREEN' ? (
          <EntityPicker label="Tela" domain="screens" value={scopeRef} onChange={setScopeRef} search={searchScreens} error={errors.scope} />
        ) : scope === 'PARTNER_PLAN' ? (
          <Field invalid={!!errors.scope}>
            <FieldLabel>ID do plano de parceiro</FieldLabel>
            <Input value={scopeId} onChange={(e) => setScopeId(e.target.value)} />
            <FieldError>{errors.scope}</FieldError>
          </Field>
        ) : null}
        <div className="grid grid-cols-2 gap-3">
          <Field invalid={!!errors.rotation}>
            <FieldLabel>Bloco interno (s)</FieldLabel>
            <Input type="number" step={30} min={30} value={internal} onChange={(e) => setInternal(e.target.value)} />
            <FieldError>{errors.rotation}</FieldError>
          </Field>
          <Field>
            <FieldLabel>Bloco externo (s)</FieldLabel>
            <Input type="number" step={30} min={30} value={external} onChange={(e) => setExternal(e.target.value)} />
          </Field>
        </div>
        <p className="m-0 text-sm text-text-muted" aria-live="polite">
          {preview ?? 'Múltiplos de 30 s; o ciclo (interno + externo) precisa dividir 3600 s.'}
        </p>
        <Field invalid={!!errors.from}>
          <FieldLabel>Vigente a partir de</FieldLabel>
          <Input type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
          <FieldDescription>
            Começa às 00:00 no {timeZoneLabel(policyTz)}{effectiveFrom ? ` (${effectiveFrom} em UTC)` : ''}.
          </FieldDescription>
          <FieldError>{errors.from}</FieldError>
        </Field>
        <Field invalid={!!errors.reason}>
          <FieldLabel required>Motivo</FieldLabel>
          <Textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={2} />
          <FieldError>{errors.reason}</FieldError>
        </Field>
        {error ? (
          <Alert variant="danger">
            <AlertDescription>{describeError(error)}</AlertDescription>
          </Alert>
        ) : null}
        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)} disabled={pending}>
            Cancelar
          </Button>
          <Button onClick={() => void save()} loading={pending}>
            Criar política
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function HoldExpiry({ h }: { h: InventoryHold }) {
  const live = h.status === 'HELD' && !!h.hold_expires_at
  const now = useNow(1000, live)
  if (!live) return <>{formatDateTime(h.hold_expires_at)}</>
  const ms = new Date(h.hold_expires_at as string).getTime() - now
  return <span className="tabular-nums">{ms > 0 ? `expira em ${formatRemaining(ms)}` : 'vencida (liberação pendente)'}</span>
}

function HoldsTab() {
  const table = useTableState({ defaultPageSize: 25, prefix: 'h_', filterKeys: ['status'] })
  const params = { page: table.state.page, page_size: table.state.pageSize, status: table.state.filters.status ?? 'HELD' }
  const q = useQuery({ queryKey: qk.list('inventory', { holds: params }), queryFn: () => inventoryApi.holds(params), placeholderData: keepPreviousData })
  const cols: DataTableColumn<InventoryHold>[] = [
    {
      id: 'campaign',
      header: 'Campanha',
      mobile: 'primary',
      cell: (h) => (
        <Link className="text-link underline-offset-4 hover:underline" to={`/campanhas/${h.campaign_id}`}>
          Campanha {shortId(h.campaign_id)}
        </Link>
      ),
    },
    { id: 'status', header: 'Status', mobile: 'badge', cell: (h) => <ToneBadge {...(HOLD_STATUS[h.status] ?? { label: h.status, tone: 'neutral' })} /> },
    { id: 'screens', header: 'Telas', numeric: true, cell: (h) => `${formatNumber(h.screens_held)} de ${formatNumber(h.screens_requested)}` },
    { id: 'expires', header: 'Expiração', cell: (h) => <HoldExpiry h={h} /> },
    { id: 'created', header: 'Criada', cell: (h) => formatDateTime(h.created_at) },
    { id: 'mode', header: 'Modo', cell: (h) => (h.replaces_hold_id ? 'Alteração/renovação' : h.mode === 'PARTIAL' ? 'Parcial' : 'Tudo ou nada') },
  ]
  return (
    <div className="flex flex-col gap-4">
      <h2 className="text-title-section">Reservas de inventário</h2>
      <p className="m-0 text-sm text-text-muted">A reserva dura 2 horas pelo relógio do banco; o pagamento a converte e a expiração libera as telas.</p>
      <DataTable
        caption="Reservas"
        columns={cols}
        data={q.data}
        getRowId={(h) => h.id}
        controller={table}
        isLoading={q.isLoading}
        isFetching={q.isFetching}
        error={q.error}
        onRetry={() => void q.refetch()}
        filtersActive={!!table.state.filters.status}
        empty={{ title: 'Nenhuma reserva ativa', description: 'Reservas aparecem quando um anunciante gera a cobrança no checkout.' }}
        toolbar={
          <FilterSelect
            label="Status"
            value={table.state.filters.status}
            onChange={(v) => table.setFilter('status', v)}
            allLabel="Reservadas (padrão)"
            options={Object.entries(HOLD_STATUS)
              .filter(([k]) => k !== 'HELD')
              .map(([k, v]) => ({ value: k, label: v.label }))}
          />
        }
      />
    </div>
  )
}

function RetirementsTab() {
  const can = useCan()
  const qc = useQueryClient()
  const table = useTableState({ defaultPageSize: 25, prefix: 'rt_', filterKeys: ['status'] })
  const params = { page: table.state.page, page_size: table.state.pageSize, status: table.state.filters.status }
  const q = useQuery({ queryKey: qk.list('inventory', { retirements: params }), queryFn: () => inventoryApi.retirements(params), placeholderData: keepPreviousData })
  const [resolving, setResolving] = React.useState<ScreenRetirement | null>(null)
  const cols: DataTableColumn<ScreenRetirement>[] = [
    { id: 'screen', header: 'Tela retirada', mobile: 'primary', cell: (r) => <span className="font-mono">{shortId(r.screen_id)}</span> },
    { id: 'est', header: 'Estabelecimento', cell: (r) => <EstablishmentName id={r.establishment_id} /> },
    { id: 'reason', header: 'Motivo', cell: (r) => r.reason ?? '—' },
    {
      id: 'affected',
      header: 'Campanhas afetadas',
      cell: (r) => (Array.isArray(r.affected) ? `${r.affected.length}` : r.affected && typeof r.affected === 'object' ? `${Object.keys(r.affected as object).length}` : '—'),
    },
    { id: 'status', header: 'Situação', mobile: 'badge', cell: (r) => <ToneBadge label={r.status === 'OPEN' ? 'Aberto' : 'Resolvido'} tone={r.status === 'OPEN' ? 'warning' : 'success'} /> },
    { id: 'created', header: 'Aberto em', cell: (r) => formatDateTime(r.created_at) },
  ]
  return (
    <div className="flex flex-col gap-4">
      <h2 className="text-title-section">Redistribuição por retirada de tela</h2>
      <CardDescription>Tela em manutenção continua no inventário; só a retirada definitiva abre este relatório para redistribuir as campanhas afetadas.</CardDescription>
      <DataTable
        caption="Relatórios de redistribuição"
        columns={cols}
        data={q.data}
        getRowId={(r) => r.id}
        controller={table}
        isLoading={q.isLoading}
        isFetching={q.isFetching}
        error={q.error}
        onRetry={() => void q.refetch()}
        filtersActive={!!table.state.filters.status}
        empty={{ title: 'Nenhuma retirada de tela pendente' }}
        rowActions={(r) =>
          r.status === 'OPEN' && can('screens.manage') ? (
            <Button variant="secondary" onClick={() => setResolving(r)}>
              Registrar resolução
            </Button>
          ) : r.resolution ? (
            <span className="text-xs text-text-muted">{r.resolution}</span>
          ) : null
        }
        toolbar={
          <FilterSelect
            label="Situação"
            value={table.state.filters.status}
            onChange={(v) => table.setFilter('status', v)}
            options={[
              { value: 'OPEN', label: 'Abertos' },
              { value: 'RESOLVED', label: 'Resolvidos' },
            ]}
          />
        }
      />
      <ReasonDialog
        open={!!resolving}
        onOpenChange={(o) => !o && setResolving(null)}
        title="Registrar a resolução da redistribuição"
        description="Descreva como as campanhas afetadas foram realocadas ou compensadas."
        reasonLabel="Resolução"
        minLength={10}
        confirmLabel="Resolver"
        onConfirm={async (text) => {
          await inventoryApi.resolveRetirement(resolving!.id, text)
          toast.success('Relatório resolvido.')
          void qc.invalidateQueries({ queryKey: qk.root('inventory') })
        }}
      />
    </div>
  )
}
