/**
 * Agendamentos (PG-13; RD): grade/calendário de uma tela gerada pelo MESMO empacotador do
 * manifesto (pré-visualização, sem gravar), no fuso da tela; estado de inventário/âncora/
 * política vigente; versões do manifesto (geração, aplicação pelo player, não alocados).
 * Backend W-INV (`/scheduler/screens/{id}/grid`, `/manifests`, `/inventory/screens/{id}/state`).
 */
import * as React from 'react'
import { useSearchParams } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardTitle } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Skeleton } from '@/components/ui/skeleton'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { EmptyState, ErrorState, NoPermission } from '@/components/patterns/states'
import { EntityPicker, type PickerOption } from '@/components/campaigns/entity-picker'
import { ToneBadge } from '@/components/campaigns/badges'
import { useTableState } from '@/hooks/useTableState'
import { qk } from '@/lib/queryKeys'
import { isApiError } from '@/lib/errors'
import { EMPTY_VALUE, formatBytes, formatDateTime, formatNumber } from '@/lib/format'
import { inventoryApi } from './lib/api'
import { searchScreens } from './lib/pickers'
import type { ManifestVersionInfo, ScreenGrid } from './lib/types'

const KIND_LABEL: Record<string, string> = { EXTERNAL: 'Anúncio', INSTITUTIONAL: 'Institucional', INTERNAL: 'Interno', FALLBACK: 'Reserva' }

function Problem({ error, onRetry, permission }: { error: unknown; onRetry: () => void; permission: string }) {
  if (isApiError(error) && error.isForbidden) return <NoPermission permission={permission} />
  return <ErrorState error={error} onRetry={onRetry} size="compact" />
}

export default function SchedulePage() {
  const [sp, setSp] = useSearchParams()
  const [screen, setScreen] = React.useState<PickerOption | null>(() => {
    const id = sp.get('tela')
    return id ? { id, label: sp.get('codigo') ?? 'Tela selecionada' } : null
  })
  const select = (s: PickerOption | null) => {
    setScreen(s)
    setSp(
      (prev) => {
        const n = new URLSearchParams(prev)
        if (s) {
          n.set('tela', s.id)
          n.set('codigo', s.label)
        } else {
          n.delete('tela')
          n.delete('codigo')
        }
        return n
      },
      { replace: true },
    )
  }
  return (
    <>
      <PageHeader title="Agendamentos" description="Programação de cada tela no fuso dela: blocos externos (anúncios) e internos, emergência e revogações." />
      <div className="flex flex-col gap-6">
        <Card>
          <div className="max-w-xl">
            <EntityPicker label="Tela" domain="screens" value={screen} onChange={select} search={searchScreens} description="Busque pelo código TEL-UF-XXXX ou nome." />
          </div>
        </Card>
        {screen ? (
          <Tabs defaultValue="grade">
            <TabsList>
              <TabsTrigger value="grade">Grade</TabsTrigger>
              <TabsTrigger value="estado">Estado e política</TabsTrigger>
              <TabsTrigger value="manifestos">Manifestos</TabsTrigger>
            </TabsList>
            <Card className="mt-6">
              <TabsContent value="grade" className="mt-0">
                <GridTab screenId={screen.id} />
              </TabsContent>
              <TabsContent value="estado" className="mt-0">
                <StateTab screenId={screen.id} />
              </TabsContent>
              <TabsContent value="manifestos" className="mt-0">
                <ManifestsTab screenId={screen.id} />
              </TabsContent>
            </Card>
          </Tabs>
        ) : (
          <EmptyState title="Escolha uma tela" description="A grade mostra exatamente o que o player vai exibir, gerada pelo servidor." />
        )}
      </div>
    </>
  )
}

function GridTab({ screenId }: { screenId: string }) {
  const [hours, setHours] = React.useState(6)
  const q = useQuery({ queryKey: qk.sub('schedule', 'grid', screenId, hours), queryFn: () => inventoryApi.grid(screenId, hours), retry: false })
  if (q.isLoading) return <Skeleton className="h-64 w-full" />
  if (q.error) return <Problem error={q.error} onRetry={() => void q.refetch()} permission="inventory.view" />
  const g = q.data as ScreenGrid
  const cycleS = Math.round(g.rotation.cycle_ms / 1000)
  const unplaced = Array.isArray(g.unplaced) ? g.unplaced.length : 0
  const hasEmergency = Array.isArray(g.emergency) ? g.emergency.length > 0 : !!g.emergency
  const revoked = Array.isArray(g.revoked) ? g.revoked.length : 0
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <CardTitle>Próximas {hours} horas</CardTitle>
        <ToggleGroup type="single" value={String(hours)} onValueChange={(v) => v && setHours(Number(v))} aria-label="Janela da grade">
          {[6, 12, 24, 48].map((h) => (
            <ToggleGroupItem key={h} value={String(h)}>
              {h} h
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
      </div>
      <CardDescription>
        Fuso da tela: {g.timezone} · ciclo de {cycleS} s · âncora {g.anchor_ms ? formatDateTime(g.anchor_ms, g.timezone) : 'ainda não definida (primeira reprodução)'} · plano{' '}
        {g.plan_sha256.slice(0, 12)}
      </CardDescription>
      {hasEmergency ? (
        <Alert variant="danger">
          <AlertDescription>Há comunicado de emergência ativo: ele tem prioridade sobre toda a programação.</AlertDescription>
        </Alert>
      ) : null}
      {unplaced > 0 ? (
        <Alert variant="warning">
          <AlertDescription>{formatNumber(unplaced)} inserção(ões) contratada(s) não couberam na grade (best effort registrado; equipe notificada).</AlertDescription>
        </Alert>
      ) : null}
      {revoked > 0 ? <p className="m-0 text-sm text-text-muted">{formatNumber(revoked)} anúncio(s) revogado(s) por kill.</p> : null}
      {g.blocks.length === 0 ? (
        <p className="m-0 text-sm text-text-muted">Sem blocos no período.</p>
      ) : (
        <div className="max-h-[32rem] overflow-y-auto">
          <Table>
            <caption className="sr-only">Grade de blocos externos</caption>
            <TableHeader>
              <TableRow className="h-11 hover:bg-transparent">
                <TableHead scope="col">Início do ciclo (hora da tela)</TableHead>
                <TableHead scope="col">Bloco externo</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {g.blocks.map((b) => (
                <TableRow key={b.bucket_ms}>
                  <TableCell className="whitespace-nowrap tabular-nums">{formatDateTime(b.bucket_ms, g.timezone)}</TableCell>
                  <TableCell>
                    {b.entries.length === 0 ? (
                      <span className="text-text-muted">Livre — preenchido com conteúdo interno</span>
                    ) : (
                      <span className="flex flex-wrap gap-1">
                        {b.entries.map((e, i) => (
                          <ToneBadge
                            key={`${e.code}-${i}`}
                            label={`${e.code} · ${KIND_LABEL[e.kind] ?? e.kind} · ${formatNumber(Math.round(e.d / 1000))} s`}
                            tone={e.kind === 'EXTERNAL' ? 'info' : 'neutral'}
                          />
                        ))}
                      </span>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  )
}

function StateTab({ screenId }: { screenId: string }) {
  const q = useQuery({ queryKey: qk.sub('schedule', 'state', screenId), queryFn: () => inventoryApi.screenState(screenId), retry: false })
  if (q.isLoading) return <Skeleton className="h-40 w-full" />
  if (q.error) return <Problem error={q.error} onRetry={() => void q.refetch()} permission="inventory.view" />
  const s = q.data as Record<string, unknown>
  const policy = (s.policy ?? {}) as Record<string, unknown>
  const tz = (s.timezone as string) ?? undefined
  const rows: Array<[string, React.ReactNode]> = [
    ['Código da tela', (s.screen_code as string) ?? EMPTY_VALUE],
    ['Fuso', tz ?? EMPTY_VALUE],
    ['Data local', (s.local_date as string) ?? EMPTY_VALUE],
    ['Vendável', s.sellable ? 'Sim' : `Não${s.unsellable_reason ? ` (${s.unsellable_reason as string})` : ''}`],
    ['Política vigente', `${policy.internal_seconds ?? '—'} s interno + ${policy.external_seconds ?? '—'} s externo · escopo ${policy.scope ?? '—'}`],
    ['Unidades de 30 s por hora', formatNumber((policy.units_per_hour as number) ?? null)],
    ['Âncora do ciclo', s.anchor_ms ? `${formatDateTime(s.anchor_ms as number, tz)} (${(s.anchor_source as string) ?? '—'})` : 'Não definida'],
    ['Versão do inventário', formatNumber((s.inventory_version as number) ?? null)],
    ['Manifesto gerado', `v${(s.manifest_version as number) ?? 0} em ${formatDateTime((s.manifest_generated_at as string) ?? null)}`],
    ['Manifesto aplicado pelo player', s.manifest_applied_version ? `v${s.manifest_applied_version as number}` : 'Nenhum confirmado'],
    ['Não alocados no último manifesto', formatNumber((s.manifest_unplaced as number) ?? null)],
  ]
  return (
    <dl className="m-0 grid grid-cols-1 gap-3 text-sm md:grid-cols-2">
      {rows.map(([k, v]) => (
        <div key={k} className="flex flex-col">
          <dt className="text-xs text-text-muted">{k}</dt>
          <dd className="m-0">{v}</dd>
        </div>
      ))}
    </dl>
  )
}

function ManifestsTab({ screenId }: { screenId: string }) {
  const table = useTableState({ defaultPageSize: 25, prefix: 'mf_' })
  const params = { page: table.state.page, page_size: table.state.pageSize }
  const q = useQuery({
    queryKey: qk.sub('schedule', 'manifests', screenId, params),
    queryFn: () => inventoryApi.manifests(screenId, params),
    placeholderData: keepPreviousData,
    retry: false,
  })
  const cols: DataTableColumn<ManifestVersionInfo>[] = [
    { id: 'v', header: 'Versão', mobile: 'primary', cell: (m) => `v${m.version}` },
    { id: 'gen', header: 'Gerado', cell: (m) => formatDateTime(m.generated_at) },
    { id: 'reason', header: 'Motivo', cell: (m) => m.reason ?? EMPTY_VALUE },
    { id: 'horizon', header: 'Horizonte', cell: (m) => `${formatDateTime(m.horizon_from)} → ${formatDateTime(m.horizon_to)}` },
    {
      id: 'applied',
      header: 'Aplicado',
      mobile: 'badge',
      cell: (m) => (m.applied_at ? <ToneBadge label={`Aplicado ${formatDateTime(m.applied_at)}`} tone="success" /> : <ToneBadge label="Não confirmado" tone="warning" />),
    },
    { id: 'unplaced', header: 'Não alocados', numeric: true, cell: (m) => formatNumber(m.unplaced_count) },
    { id: 'size', header: 'Tamanho', numeric: true, cell: (m) => formatBytes(m.size_bytes) },
  ]
  if (q.error && isApiError(q.error) && q.error.isForbidden) return <NoPermission permission="screens.view" />
  return (
    <DataTable
      caption="Versões do manifesto"
      columns={cols}
      data={q.data}
      getRowId={(m) => String(m.version)}
      controller={table}
      isLoading={q.isLoading}
      isFetching={q.isFetching}
      error={q.error}
      onRetry={() => void q.refetch()}
      empty={{ title: 'Nenhum manifesto gerado para esta tela', description: 'O manifesto é gerado quando há programação e a tela está pareada.' }}
    />
  )
}
