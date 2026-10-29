/**
 * Relatórios (DS §11, §13; P001 §103; ETAPA2 #785–#806; REQ-REL-001/002/003/005).
 * Catálogo conforme o público decidido pelo servidor (equipe RD / estabelecimento /
 * anunciante), filtros (período, campanha, tela, tipo de conteúdo, agrupamento), visualização
 * (gráfico com resumo + tabela), exportação PDF/XLSX/CSV com download autenticado, envio por
 * e-mail e o relatório mensal automático. "Exibições válidas comprovadas" (proof-of-play) ≠
 * programação planejada; audiência só aparece como ESTIMADA no comprovante.
 */
import * as React from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { BarChart3, CalendarClock, Download, FileSpreadsheet, FileText, Mail, ShieldCheck } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Field, FieldLabel } from '@/components/ui/field'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Skeleton } from '@/components/ui/skeleton'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { EmptyState, ErrorState } from '@/components/patterns/states'
import { ChartFrame, ChartLegend } from '@/components/charts/chart-frame'
import { TimeSeriesChart } from '@/components/charts/time-series-chart'
import { BarChart } from '@/components/charts/bar-chart'
import { useChartTheme } from '@/components/charts/chart-theme'
import { PeriodSelector } from '@/components/finance/period-selector'
import { formatIsoDate, formatIsoDayMonth, periodLabel, usePeriodState } from '@/components/finance/period'
import { useContextOrgId } from '@/components/finance/hooks'
import { useDownload } from '@/components/finance/use-download'
import { useTableState } from '@/hooks/useTableState'
import { useCan } from '@/hooks/usePermissions'
import { useActiveRouteContext } from '@/stores/contextStore'
import { http, type Paginated } from '@/lib/http'
import { qk } from '@/lib/queryKeys'
import { errorMessage } from '@/lib/errors'
import { formatBytes, formatDateTime, formatNumber } from '@/lib/format'
import { apiPath, reportsApi, sumColumn, type ContentKind, type ReportAudience, type ReportExport, type ReportParams } from './api'
import { ReportTable } from './report-view'
import { ProofsPanel } from './proofs-panel'

const KINDS: Array<{ value: ContentKind; label: string }> = [
  { value: 'EXTERNAL', label: 'Anúncios (externo)' },
  { value: 'INTERNAL', label: 'Conteúdo do estabelecimento' },
  { value: 'INSTITUTIONAL', label: 'Institucional RD' },
  { value: 'FALLBACK', label: 'Padrão RD' },
]
const COMMISSION_GROUPS = [
  { value: 'day', label: 'Dia' },
  { value: 'month', label: 'Mês' },
  { value: 'screen', label: 'Tela' },
]
const FORMATS: Array<{ value: 'pdf' | 'xlsx' | 'csv'; label: string; icon: typeof FileText }> = [
  { value: 'pdf', label: 'PDF', icon: FileText },
  { value: 'xlsx', label: 'XLSX', icon: FileSpreadsheet },
  { value: 'csv', label: 'CSV', icon: FileSpreadsheet },
]

interface Lite {
  id: string
  name: string
  friendly_id?: string
}

function useAudience(): { audience: ReportAudience; orgId: string | null } {
  const ctx = useActiveRouteContext()
  const orgId = useContextOrgId()
  if (ctx === 'establishment') return { audience: 'ESTABELECIMENTO', orgId }
  if (ctx === 'advertiser') return { audience: 'ANUNCIANTE', orgId }
  return { audience: 'EQUIPE', orgId: null }
}

function ReportChart({ reportKey, rows, title }: { reportKey: string; rows: Array<Record<string, string | number | null>>; title: string }) {
  const theme = useChartTheme()
  if (reportKey === 'exibicoes_por_dia') {
    const data = rows.map((r) => ({ day: String(r.day ?? ''), valid: typeof r.valid === 'number' ? r.valid : null }))
    return (
      <ChartFrame
        title={title}
        summary={`${formatNumber(sumColumn(rows, 'valid'))} exibições válidas em ${rows.length} dias.`}
        empty={!data.length}
        emptyText="Nenhuma exibição registrada neste período."
        table={{ caption: title, columns: [{ key: 'day', label: 'Dia', render: (r) => formatIsoDate(r.day) }, { key: 'valid', label: 'Exibições válidas', numeric: true, render: (r) => formatNumber(r.valid) }], rows: data }}
        legend={<ChartLegend items={[{ label: 'Exibições válidas', color: theme.series[0] }]} />}
      >
        <TimeSeriesChart data={data} xKey="day" series={[{ key: 'valid', label: 'Exibições válidas' }]} formatX={(v) => formatIsoDayMonth(v)} formatValue={(v) => `${formatNumber(v)} exibições`} />
      </ChartFrame>
    )
  }
  const labelKey = reportKey === 'exibicoes_por_horario' ? 'hour' : reportKey === 'exibicoes_por_regiao' ? 'region' : reportKey === 'exibicoes_por_tipo' ? 'kind' : null
  if (!labelKey) return null
  const data = rows.map((r) => ({ label: String(r[labelKey] ?? '—'), value: typeof r.valid === 'number' ? r.valid : 0 }))
  return (
    <ChartFrame
      title={title}
      summary={`${formatNumber(sumColumn(rows, 'valid'))} exibições válidas em ${rows.length} grupos.`}
      empty={!data.length}
      emptyText="Nenhuma exibição registrada neste período."
      table={{ caption: title, columns: [{ key: 'l', label: 'Grupo', render: (r) => r.label }, { key: 'v', label: 'Exibições válidas', numeric: true, render: (r) => formatNumber(r.value) }], rows: data }}
      legend={<ChartLegend items={[{ label: 'Exibições válidas', color: theme.series[0], kind: 'swatch' }]} />}
    >
      <BarChart data={data} orientation={reportKey === 'exibicoes_por_horario' ? 'vertical' : 'horizontal'} formatValue={(v) => `${formatNumber(v)} exibições`} />
    </ChartFrame>
  )
}

function ExportsHistory() {
  const dl = useDownload()
  const table = useTableState({ defaultPageSize: 10, prefix: 'e_' })
  const params = { page: table.state.page, page_size: table.state.pageSize }
  const q = useQuery({ queryKey: qk.sub('reports', 'exports', params), queryFn: () => reportsApi.exports(params), placeholderData: keepPreviousData })
  const cols: DataTableColumn<ReportExport>[] = [
    { id: 'created', header: 'Gerado em', mobile: 'secondary', cell: (e) => formatDateTime(e.created_at) },
    { id: 'file', header: 'Arquivo', mobile: 'primary', cell: (e) => <span className="font-mono text-xs">{e.filename ?? e.report_key}</span> },
    { id: 'rows', header: 'Linhas', numeric: true, mobile: 'field', cell: (e) => formatNumber(e.row_count) },
    { id: 'size', header: 'Tamanho', numeric: true, mobile: 'field', cell: (e) => formatBytes(e.size_bytes) },
    { id: 'email', header: 'E-mail', mobile: 'field', cell: (e) => (e.emailed_to ? 'Enviado' : '—') },
    { id: 'expires', header: 'Disponível até', mobile: 'field', cell: (e) => formatDateTime(e.expires_at) },
  ]
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Minhas exportações</CardTitle>
          <CardDescription>Arquivos gerados nos últimos 30 dias (somente você pode baixar).</CardDescription>
        </div>
      </CardHeader>
      <DataTable
        caption="Minhas exportações"
        columns={cols}
        data={q.data}
        getRowId={(e) => e.id}
        controller={table}
        isLoading={q.isLoading}
        isFetching={q.isFetching}
        error={q.error}
        onRetry={() => void q.refetch()}
        empty={{ title: 'Nenhuma exportação ainda.' }}
        rowActions={(e) => (
          <Button
            variant="secondary"
            loading={dl.isPending(e.id)}
            loadingText="Baixando…"
            onClick={() => void dl.run(e.id, apiPath(e.download_url), { fallbackFilename: e.filename ?? `${e.report_key}.${e.format}` })}
            aria-label={`Baixar ${e.filename ?? e.report_key}`}
          >
            <Download aria-hidden="true" />
            Baixar
          </Button>
        )}
      />
    </Card>
  )
}

function ReportsWorkspace() {
  const qc = useQueryClient()
  const can = useCan()
  const dl = useDownload()
  const { audience, orgId } = useAudience()
  const [sp, setSp] = useSearchParams()
  const { period, setPeriod } = usePeriodState({ defaultPreset: '30' })
  const [emailMe, setEmailMe] = React.useState(false)
  const reportKey = sp.get('relatorio')
  const campaignId = sp.get('campanha') ?? undefined
  const screenId = sp.get('tela') ?? undefined
  const kind = (sp.get('tipo') as ContentKind | null) ?? undefined
  const groupBy = sp.get('agrupar') ?? undefined

  const setParam = (name: string, value: string | null) =>
    setSp(
      (prev) => {
        const n = new URLSearchParams(prev)
        if (value) n.set(name, value)
        else n.delete(name)
        return n
      },
      { replace: true },
    )

  const base = { audience, org_id: orgId ?? undefined }
  const catalog = useQuery({ queryKey: qk.sub('reports', 'catalog', base), queryFn: () => reportsApi.catalog(base) })
  const items = catalog.data?.items ?? []
  const current = items.find((i) => i.key === reportKey) ?? items[0] ?? null
  const isCommission = current?.key === 'comissao'
  const params: ReportParams = {
    ...base,
    start: period.start,
    end: period.end,
    campaign_id: audience !== 'ESTABELECIMENTO' ? campaignId : undefined,
    screen_id: audience !== 'ANUNCIANTE' ? screenId : undefined,
    kind: audience !== 'ANUNCIANTE' ? kind : undefined,
    group_by: isCommission ? groupBy : undefined,
  }
  const data = useQuery({
    queryKey: qk.sub('reports', 'data', current?.key ?? '', params),
    queryFn: () => reportsApi.data(current!.key, params),
    enabled: !!current,
    placeholderData: keepPreviousData,
  })
  const campaigns = useQuery({
    queryKey: qk.list('campaigns', { org_id: orgId ?? undefined, page_size: 100, picker: 'reports' }),
    queryFn: () => http.get<Paginated<Lite>>('/campaigns', { query: { org_id: orgId ?? undefined, page_size: 100 } }),
    enabled: audience !== 'ESTABELECIMENTO' && can('campaigns.view'),
  })
  const screens = useQuery({
    queryKey: qk.list('screens', { org_id: orgId ?? undefined, page_size: 100, picker: 'reports' }),
    queryFn: () => http.get<Paginated<Lite>>('/screens', { query: { org_id: orgId ?? undefined, page_size: 100 } }),
    enabled: audience !== 'ANUNCIANTE' && can('screens.view'),
  })

  const exp = useMutation({
    mutationFn: (format: 'pdf' | 'xlsx' | 'csv') => reportsApi.createExport({ ...params, report_key: current!.key, format, email_to_me: emailMe }),
    onSuccess: async (e) => {
      if (e.emailed_to) toast.success('Relatório enviado para o seu e-mail.')
      // Só o histórico de exportações muda (catálogo e dados continuam válidos).
      await qc.invalidateQueries({ queryKey: qk.sub('reports', 'exports') })
      await dl.run(e.id, apiPath(e.download_url), { fallbackFilename: e.filename ?? `${e.report_key}.${e.format}` })
    },
    onError: (e) => toast.error(errorMessage(e)),
  })
  const canExport = audience !== 'EQUIPE' || can('reports.export')

  if (catalog.isLoading) return <Skeleton className="h-64 w-full" />
  if (catalog.error) return <ErrorState error={catalog.error} onRetry={() => void catalog.refetch()} />
  if (!current) return <EmptyState icon={BarChart3} title="Nenhum relatório disponível para o seu perfil." />

  const d = data.data
  return (
    <div className="flex flex-col gap-6">
      <Alert variant="info">
        <ShieldCheck aria-hidden="true" />
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <AlertTitle>Exibições válidas comprovadas</AlertTitle>
          <AlertDescription>
            Os números são reproduções confirmadas pelos players (proof-of-play, 100% da duração) — não a programação planejada. Exibição não é pessoa.
            {audience === 'ANUNCIANTE' ? ' Telas aparecem por código e região, sem nome ou endereço do local.' : ''}
            {audience === 'ESTABELECIMENTO' ? ' Por política comercial, anunciantes e campanhas não são identificados.' : ''}
          </AlertDescription>
        </div>
      </Alert>
      <Card>
        <div className="flex flex-wrap items-end gap-x-6 gap-y-4">
          <Field className="w-full max-w-xs">
            <FieldLabel>Relatório</FieldLabel>
            <Select value={current.key} onValueChange={(v) => setParam('relatorio', v)}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  {items.map((i) => (
                    <SelectItem key={i.key} value={i.key}>
                      {i.label}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
          </Field>
          {audience !== 'ESTABELECIMENTO' && !isCommission ? (
            <Field className="w-full max-w-xs">
              <FieldLabel>Campanha</FieldLabel>
              <Select value={campaignId ?? 'all'} onValueChange={(v) => setParam('campanha', v === 'all' ? null : v)}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    <SelectItem value="all">Todas</SelectItem>
                    {(campaigns.data?.items ?? []).map((c) => (
                      <SelectItem key={c.id} value={c.id}>
                        {c.name}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
            </Field>
          ) : null}
          {audience !== 'ANUNCIANTE' && !isCommission ? (
            <>
              <Field className="w-full max-w-xs">
                <FieldLabel>Tela</FieldLabel>
                <Select value={screenId ?? 'all'} onValueChange={(v) => setParam('tela', v === 'all' ? null : v)}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      <SelectItem value="all">Todas</SelectItem>
                      {(screens.data?.items ?? []).map((s) => (
                        <SelectItem key={s.id} value={s.id}>
                          {[s.friendly_id, s.name].filter(Boolean).join(' · ')}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  </SelectContent>
                </Select>
              </Field>
              <Field className="w-full max-w-[240px]">
                <FieldLabel>Tipo de conteúdo</FieldLabel>
                <Select value={kind ?? 'all'} onValueChange={(v) => setParam('tipo', v === 'all' ? null : v)}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      <SelectItem value="all">Todos</SelectItem>
                      {KINDS.map((k) => (
                        <SelectItem key={k.value} value={k.value}>
                          {k.label}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  </SelectContent>
                </Select>
              </Field>
            </>
          ) : null}
          {isCommission ? (
            <Field className="w-full max-w-[200px]">
              <FieldLabel>Agrupar por</FieldLabel>
              <Select value={groupBy ?? 'day'} onValueChange={(v) => setParam('agrupar', v === 'day' ? null : v)}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    {COMMISSION_GROUPS.map((g) => (
                      <SelectItem key={g.value} value={g.value}>
                        {g.label}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
            </Field>
          ) : null}
        </div>
        <PeriodSelector value={period} onChange={setPeriod} />
      </Card>

      <Card>
        <CardHeader>
          <div className="flex flex-col gap-1">
            <CardTitle>{d?.title ?? current.label}</CardTitle>
            <CardDescription>{periodLabel(period)}</CardDescription>
          </div>
          {canExport ? (
            <div className="flex flex-wrap items-center gap-2">
              {FORMATS.map((f) => (
                <Button
                  key={f.value}
                  variant="secondary"
                  disabled={exp.isPending || !d}
                  loading={exp.isPending && exp.variables === f.value}
                  loadingText="Gerando…"
                  onClick={() => exp.mutate(f.value)}
                  aria-label={`Exportar em ${f.label}`}
                >
                  <f.icon aria-hidden="true" />
                  {f.label}
                </Button>
              ))}
            </div>
          ) : null}
        </CardHeader>
        {canExport ? (
          <Field orientation="horizontal" className="min-h-11 flex-nowrap">
            <Checkbox checked={emailMe} onCheckedChange={(v) => setEmailMe(v === true)} />
            <FieldLabel className="min-w-0 flex-1 font-medium">
              <Mail aria-hidden="true" className="mr-1 inline size-4" />
              Também enviar o arquivo para o meu e-mail
            </FieldLabel>
          </Field>
        ) : (
          <p className="m-0 text-xs text-text-muted">Exportar exige a permissão reports.export.</p>
        )}
        {data.isLoading ? (
          <Skeleton className="h-64 w-full" />
        ) : data.error ? (
          <ErrorState error={data.error} onRetry={() => void data.refetch()} size="compact" />
        ) : d && d.rows.length === 0 ? (
          <EmptyState size="compact" headingLevel="h3" icon={BarChart3} title="Nenhuma exibição registrada neste período." description="Ajuste o período ou os filtros." />
        ) : d ? (
          <div className="flex flex-col gap-4">
            {d.meta.length ? (
              <ul className="m-0 flex list-none flex-wrap gap-x-6 gap-y-1 p-0 text-xs text-text-muted">
                {d.meta.map((m) => (
                  <li key={m.label}>
                    {m.label}: <span className="text-text">{m.value}</span>
                  </li>
                ))}
              </ul>
            ) : null}
            <ReportChart reportKey={current.key} rows={d.rows} title={d.title} />
            <ReportTable data={d} />
            {d.notes.length ? (
              <ul className="m-0 flex list-disc flex-col gap-1 pl-5 text-xs text-text-muted">
                {d.notes.map((n) => (
                  <li key={n}>{n}</li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}
      </Card>

      <Card>
        <CardHeader>
          <div className="flex flex-col gap-1">
            <CardTitle>
              <CalendarClock aria-hidden="true" className="mr-1 inline size-4" />
              Relatório mensal automático
            </CardTitle>
            <CardDescription>
              Todo dia 1 a plataforma envia por e-mail o PDF do mês anterior (exibições válidas) a anunciantes e estabelecimentos com acesso a relatórios — sem duplicar envios.
            </CardDescription>
          </div>
        </CardHeader>
        <p className="m-0 text-sm text-text">
          Para deixar de receber ou ajustar o canal, use as{' '}
          <Link to="/notificacoes" className="font-semibold text-link underline-offset-4 hover:underline">
            preferências de notificação
          </Link>
          .
        </p>
      </Card>
      <ExportsHistory />
    </div>
  )
}

export default function ReportsPage() {
  const ctx = useActiveRouteContext()
  const can = useCan()
  const showProofs = ctx === 'rd' && (can('reports.view') || can('campaigns.view'))
  const [sp, setSp] = useSearchParams()
  const tab = sp.get('aba') === 'comprovantes' && showProofs ? 'comprovantes' : 'relatorios'
  const title = ctx === 'establishment' ? 'Relatório de exibições' : 'Relatórios'

  return (
    <div className="flex flex-col gap-6">
      <PageHeader title={title} description="Exibições válidas, falhas, uptime, comissão e financeiro; exportação PDF/XLSX/CSV" />
      {showProofs ? (
        <Tabs
          value={tab}
          onValueChange={(v) =>
            setSp(
              (prev) => {
                const n = new URLSearchParams(prev)
                if (v === 'comprovantes') n.set('aba', 'comprovantes')
                else n.delete('aba')
                return n
              },
              { replace: true },
            )
          }
        >
          <TabsList>
            <TabsTrigger value="relatorios">Relatórios</TabsTrigger>
            <TabsTrigger value="comprovantes">Comprovantes de veiculação</TabsTrigger>
          </TabsList>
          <TabsContent value="relatorios">
            <ReportsWorkspace />
          </TabsContent>
          <TabsContent value="comprovantes">
            <ProofsPanel />
          </TabsContent>
        </Tabs>
      ) : (
        <ReportsWorkspace />
      )}
    </div>
  )
}
