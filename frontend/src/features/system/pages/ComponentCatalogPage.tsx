/**
 * SOMENTE DESENVOLVIMENTO (/_dev/componentes) — vitrine dos componentes da fundação para a
 * revisão visual (P001 §136) e referência dos agentes de páginas. TODOS os valores aqui são
 * DEMONSTRAÇÃO e estão rotulados como tal. A rota não existe no build de produção.
 */
import * as React from 'react'
import { toast } from 'sonner'
import { Plus, Trash2, MoreHorizontal, Pencil, Download, HelpCircle } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Checkbox } from '@/components/ui/checkbox'
import { Switch } from '@/components/ui/switch'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel, FieldLegend, FieldSet } from '@/components/ui/field'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog'
import { Sheet, SheetBody, SheetContent, SheetDescription, SheetHeader, SheetTitle, SheetTrigger } from '@/components/ui/sheet'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { StatusBadge } from '@/components/patterns/status-badge'
import { STATUS_PRESETS, type StatusKind } from '@/components/patterns/status-presets'
import { KpiCard } from '@/components/patterns/kpi-card'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { ConfirmDialog } from '@/components/patterns/confirm-dialog'
import { FileDropzone, type UploadItem } from '@/components/patterns/file-dropzone'
import { DemoBadge, EmptyState, ErrorState, NoPermission, StaleData } from '@/components/patterns/states'
import { ChartFrame, ChartLegend } from '@/components/charts/chart-frame'
import { TimeSeriesChart } from '@/components/charts/time-series-chart'
import { DonutChart } from '@/components/charts/donut-chart'
import { BarChart } from '@/components/charts/bar-chart'
import { AvailabilityRing } from '@/components/charts/availability-ring'
import { SERIES_DASH, useChartTheme } from '@/components/charts/chart-theme'
import { useTableState } from '@/hooks/useTableState'
import { ApiError } from '@/lib/errors'
import { formatDate, formatNumber } from '@/lib/format'

// ---------------- Dados de DEMONSTRAÇÃO (fictícios) ----------------
interface DemoScreen {
  id: string
  name: string
  establishment: string
  status: StatusKind
  lastContact: string
  plays: number
}
const DEMO_SCREENS: DemoScreen[] = Array.from({ length: 23 }, (_, i) => ({
  id: `TEL-XX-DEMO${String(i).padStart(2, '0')}`,
  name: `Tela demonstração ${i + 1}`,
  establishment: `Estabelecimento demonstração ${(i % 5) + 1}`,
  status: (['online', 'offline', 'syncing', 'online', 'paused'] as StatusKind[])[i % 5],
  lastContact: new Date(Date.UTC(2026, 9, 7, 12, i * 2)).toISOString(),
  plays: (i * 37) % 400,
}))
const DEMO_SERIES = Array.from({ length: 14 }, (_, i) => ({
  dia: new Date(Date.UTC(2026, 8, 24 + i)).toISOString(),
  exibicoes: 800 + ((i * 131) % 500),
  sincronizacoes: 300 + ((i * 89) % 260),
}))
const DEMO_SPLIT = [
  { label: 'Demo A', value: 420 },
  { label: 'Demo B', value: 260 },
  { label: 'Demo C', value: 140 },
  { label: 'Demo D', value: 60 },
  { label: 'Demo E', value: 40 },
]

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-4" aria-labelledby={`sec-${title}`}>
      <h2 id={`sec-${title}`} className="text-title-section text-text">
        {title}
      </h2>
      {children}
    </section>
  )
}

function DemoTable() {
  const table = useTableState({ defaultPageSize: 10, filterKeys: ['status'], prefix: 'demo_' })
  const { page, pageSize, sort, q, filters } = table.state
  const filtered = DEMO_SCREENS.filter(
    (s) => (!q || s.name.toLowerCase().includes(q.toLowerCase())) && (!filters.status || s.status === filters.status),
  )
  const sorted = [...filtered].sort((a, b) => {
    if (!sort) return 0
    const dir = sort.startsWith('-') ? -1 : 1
    const key = sort.replace('-', '') as keyof DemoScreen
    return String(a[key]).localeCompare(String(b[key]), 'pt-BR', { numeric: true }) * dir
  })
  const data = { items: sorted.slice((page - 1) * pageSize, page * pageSize), total: sorted.length, page, page_size: pageSize }
  const columns: DataTableColumn<DemoScreen>[] = [
    { id: 'name', header: 'Tela', sortField: 'name', mobile: 'primary', cell: (r) => <span className="font-semibold">{r.name}</span> },
    { id: 'id', header: 'ID', mobile: 'secondary', cell: (r) => <code className="text-xs">{r.id}</code> },
    { id: 'est', header: 'Estabelecimento', sortField: 'establishment', cell: (r) => r.establishment },
    { id: 'status', header: 'Status', mobile: 'badge', cell: (r) => <StatusBadge status={r.status} /> },
    { id: 'last', header: 'Último contato', sortField: 'lastContact', cell: (r) => formatDate(r.lastContact) },
    { id: 'plays', header: 'Exibições', numeric: true, sortField: 'plays', cell: (r) => formatNumber(r.plays) },
  ]
  return (
    <DataTable
      caption="Telas (demonstração)"
      columns={columns}
      data={data}
      getRowId={(r) => r.id}
      controller={table}
      filtersActive={!!q || !!filters.status}
      empty={{ title: 'Adicione sua primeira tela para começar.', description: 'Demonstração do estado vazio.' }}
      rowActions={(r) => (
        <Button variant="ghost" size="icon" aria-label={`Editar ${r.name}`}>
          <Pencil aria-hidden="true" />
        </Button>
      )}
      toolbar={
        <>
          <Field className="w-full max-w-xs">
            <FieldLabel>Buscar</FieldLabel>
            <Input value={q} onChange={(e) => table.setQuery(e.target.value)} placeholder="Nome da tela" />
          </Field>
          <Field className="w-full max-w-[220px]">
            <FieldLabel>Status</FieldLabel>
            <Select value={filters.status ?? 'all'} onValueChange={(v) => table.setFilter('status', v === 'all' ? null : v)}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  <SelectItem value="all">Todos</SelectItem>
                  {(['online', 'offline', 'syncing', 'paused'] as StatusKind[]).map((s) => (
                    <SelectItem key={s} value={s}>
                      {STATUS_PRESETS[s].label}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
          </Field>
        </>
      }
    />
  )
}

export default function ComponentCatalogPage() {
  const [confirmOpen, setConfirmOpen] = React.useState(false)
  const [loadingBtn, setLoadingBtn] = React.useState(false)
  const [items, setItems] = React.useState<UploadItem[]>([])
  const theme = useChartTheme()

  return (
    <div className="flex flex-col gap-12">
      <PageHeader
        title="Catálogo de componentes"
        description={<DemoBadge>Dados de demonstração</DemoBadge>}
        actions={
          <Button onClick={() => toast.success('Toast de demonstração.')}>
            <Plus aria-hidden="true" />
            Ação principal
          </Button>
        }
      />

      <Section title="Botões">
        <Card>
          <div className="flex flex-wrap items-center gap-3">
            <Button>Primário</Button>
            <Button variant="secondary">Secundário</Button>
            <Button variant="tertiary">Terciário</Button>
            <Button variant="destructive">
              <Trash2 aria-hidden="true" />
              Destrutivo
            </Button>
            <Button variant="ghost" size="icon" aria-label="Mais opções">
              <MoreHorizontal aria-hidden="true" />
            </Button>
            <Button disabled>Desabilitado</Button>
            <Button variant="secondary" disabled>
              Secundário desabilitado
            </Button>
            <Button
              loading={loadingBtn}
              onClick={() => {
                setLoadingBtn(true)
                window.setTimeout(() => setLoadingBtn(false), 1500)
              }}
            >
              Salvar (loading)
            </Button>
          </div>
        </Card>
      </Section>

      <Section title="Formulários">
        <Card>
          <FieldGroup className="max-w-xl">
            <Field>
              <FieldLabel required>Nome do estabelecimento</FieldLabel>
              <Input placeholder="Ex.: Mercado Central" />
              <FieldDescription>Nome exibido para a equipe RD.</FieldDescription>
            </Field>
            <Field invalid>
              <FieldLabel required>E-mail de contato</FieldLabel>
              <Input defaultValue="contato@" />
              <FieldError>Informe um e-mail válido.</FieldError>
            </Field>
            <Field>
              <FieldLabel>Fuso horário</FieldLabel>
              <Select defaultValue="America/Sao_Paulo">
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    <SelectItem value="America/Sao_Paulo">America/Sao_Paulo</SelectItem>
                    <SelectItem value="America/Manaus">America/Manaus</SelectItem>
                    <SelectItem value="America/Cuiaba">America/Cuiaba</SelectItem>
                  </SelectGroup>
                </SelectContent>
              </Select>
            </Field>
            <Field>
              <FieldLabel>Observações</FieldLabel>
              <Textarea />
            </Field>
            <Field disabled>
              <FieldLabel>Campo desabilitado</FieldLabel>
              <Input defaultValue="Somente leitura" />
            </Field>
            <FieldSet>
              <FieldLegend>Orientação da tela</FieldLegend>
              <RadioGroup defaultValue="h" aria-label="Orientação da tela">
                <Field orientation="horizontal" className="min-h-11">
                  <RadioGroupItem value="h" id="or-h" />
                  <FieldLabel htmlFor="or-h" className="font-medium">
                    Horizontal (1920 × 1080)
                  </FieldLabel>
                </Field>
                <Field orientation="horizontal" className="min-h-11">
                  <RadioGroupItem value="v" id="or-v" />
                  <FieldLabel htmlFor="or-v" className="font-medium">
                    Vertical (1080 × 1920)
                  </FieldLabel>
                </Field>
              </RadioGroup>
            </FieldSet>
            <Field orientation="horizontal" className="min-h-11">
              <Checkbox defaultChecked />
              <FieldLabel className="font-medium">Aprovação automática</FieldLabel>
            </Field>
            <Field orientation="horizontal" className="min-h-11">
              <Switch defaultChecked />
              <FieldLabel className="font-medium">Notificar por e-mail</FieldLabel>
            </Field>
            <Field>
              <FieldLabel id="periodo-label">Período</FieldLabel>
              <ToggleGroup type="single" defaultValue="7" aria-labelledby="periodo-label" className="self-start">
                <ToggleGroupItem value="hoje">Hoje</ToggleGroupItem>
                <ToggleGroupItem value="7">7 dias</ToggleGroupItem>
                <ToggleGroupItem value="30">30 dias</ToggleGroupItem>
                <ToggleGroupItem value="custom">Personalizado</ToggleGroupItem>
              </ToggleGroup>
            </Field>
          </FieldGroup>
        </Card>
      </Section>

      <Section title="Status">
        <Card>
          <div className="flex flex-wrap gap-2">
            {(Object.keys(STATUS_PRESETS) as StatusKind[]).map((s) => (
              <StatusBadge key={s} status={s} />
            ))}
          </div>
        </Card>
      </Section>

      <Section title="KPIs">
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4 xl:gap-6">
          <KpiCard demo label="Telas online" value={128} unit="telas" comparison={{ deltaPct: 4.2, periodLabel: 'vs. 7 dias anteriores' }} trend={[110, 115, 112, 120, 118, 125, 128]} />
          <KpiCard demo label="Falhas de reprodução" value={12} comparison={{ deltaPct: -18.5, periodLabel: 'vs. 7 dias anteriores', polarity: 'lower-is-better' }} />
          <KpiCard demo label="Anunciantes ativos" value={34} comparison={{ deltaPct: null, periodLabel: 'vs. mês anterior' }} />
          <KpiCard demo label="Receita conciliada" value={null} description="Sem dados financeiros conciliados no período." />
        </div>
      </Section>

      <Section title="Gráficos">
        <div className="grid grid-cols-1 gap-6 xl:grid-cols-12">
          <ChartFrame
            className="xl:col-span-8"
            demo
            title="Exibições registradas por dia"
            description="Últimos 14 dias · America/Sao_Paulo"
            summary="Demonstração: exibições variaram entre 800 e 1.299 por dia; sincronizações acompanharam a tendência."
            legend={
              <ChartLegend
                items={[
                  { label: 'Exibições', color: theme.series[0], dash: SERIES_DASH[0] },
                  { label: 'Sincronizações', color: theme.series[1], dash: SERIES_DASH[1] },
                ]}
              />
            }
            table={{
              caption: 'Exibições e sincronizações por dia (demonstração)',
              rows: DEMO_SERIES,
              columns: [
                { key: 'dia', label: 'Dia', render: (r) => formatDate(r.dia) },
                { key: 'exib', label: 'Exibições', numeric: true, render: (r) => formatNumber(r.exibicoes) },
                { key: 'sync', label: 'Sincronizações', numeric: true, render: (r) => formatNumber(r.sincronizacoes) },
              ],
            }}
          >
            <TimeSeriesChart
              data={DEMO_SERIES}
              xKey="dia"
              series={[
                { key: 'exibicoes', label: 'Exibições' },
                { key: 'sincronizacoes', label: 'Sincronizações' },
              ]}
              formatX={(v) => formatDate(String(v)).slice(0, 5)}
              formatValue={(v) => `${formatNumber(v)}`}
              formatTick={(v) => formatNumber(v, { notation: 'compact' })}
            />
          </ChartFrame>
          <ChartFrame
            className="xl:col-span-4"
            demo
            title="Distribuição por estabelecimento"
            summary="Demonstração: Demo A concentra 45% das exibições; categorias menores agrupadas em Outros."
            table={{
              rows: DEMO_SPLIT,
              columns: [
                { key: 'l', label: 'Estabelecimento', render: (r) => r.label },
                { key: 'v', label: 'Exibições', numeric: true, render: (r) => formatNumber(r.value) },
              ],
            }}
          >
            <DonutChart data={DEMO_SPLIT} />
          </ChartFrame>
          <ChartFrame
            className="xl:col-span-8"
            demo
            title="Exibições por estabelecimento"
            summary="Demonstração: barras a partir de zero, valor na ponta."
            table={{
              rows: DEMO_SPLIT,
              columns: [
                { key: 'l', label: 'Estabelecimento', render: (r) => r.label },
                { key: 'v', label: 'Exibições', numeric: true, render: (r) => formatNumber(r.value) },
              ],
            }}
          >
            <BarChart data={DEMO_SPLIT} />
          </ChartFrame>
          <ChartFrame
            className="xl:col-span-4"
            demo
            title="Disponibilidade das telas"
            summary="Demonstração: 97,4% do tempo monitorado com as telas online."
            table={{ rows: [{ v: 97.4 }], columns: [{ key: 'v', label: 'Disponibilidade', numeric: true, render: (r) => `${formatNumber(r.v)}%` }] }}
          >
            <div className="flex justify-center py-2">
              <AvailabilityRing value={97.4} />
            </div>
          </ChartFrame>
          <ChartFrame className="xl:col-span-6" title="Estado carregando" summary="" loading table={{ rows: [], columns: [] }}>
            {null}
          </ChartFrame>
          <ChartFrame className="xl:col-span-6" title="Relatório vazio" summary="" empty emptyText="Nenhuma exibição registrada neste período." table={{ rows: [], columns: [] }}>
            {null}
          </ChartFrame>
        </div>
      </Section>

      <Section title="Tabela de dados">
        <Card>
          <DemoTable />
        </Card>
      </Section>

      <Section title="Estados">
        <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
          <Card>
            <EmptyState size="compact" headingLevel="h3" title="Envie imagens ou vídeos para montar suas campanhas." action={<Button>Enviar mídia</Button>} />
          </Card>
          <Card>
            <ErrorState
              size="compact"
              error={new ApiError({ status: 503, code: 'HTTP_503', message: 'Erro no servidor. Tente novamente em alguns instantes.', requestId: 'demo-req-0001' })}
              onRetry={() => toast('Demonstração: nova tentativa.')}
            />
          </Card>
          <Card>
            <NoPermission permission="finance.approve" />
          </Card>
          <Card>
            <StaleData updatedAt={new Date(Date.UTC(2026, 9, 7, 14, 32))} reason="tempo real indisponível." onRefresh={() => toast('Demonstração: atualizar.')} />
            <Alert variant="info">
              <AlertTitle>Campanha publicada.</AlertTitle>
              <AlertDescription>Aguardando sincronização das telas.</AlertDescription>
            </Alert>
            <Alert variant="danger">
              <AlertTitle>Sem comunicação.</AlertTitle>
              <AlertDescription>Último contato: 07/10/2026 11:32 (demonstração).</AlertDescription>
            </Alert>
          </Card>
        </div>
      </Section>

      <Section title="Sobreposições">
        <Card>
          <div className="flex flex-wrap gap-3">
            <Dialog>
              <DialogTrigger asChild>
                <Button variant="secondary">Abrir modal</Button>
              </DialogTrigger>
              <DialogContent>
                <DialogHeader>
                  <DialogTitle>Editar tela</DialogTitle>
                  <DialogDescription>O foco fica preso aqui; Esc fecha e devolve o foco ao botão.</DialogDescription>
                </DialogHeader>
                <Field>
                  <FieldLabel>Nome</FieldLabel>
                  <Input defaultValue="Tela demonstração 1" />
                </Field>
                <DialogFooter>
                  <DialogClose asChild>
                    <Button variant="secondary">Cancelar</Button>
                  </DialogClose>
                  <DialogClose asChild>
                    <Button>Salvar</Button>
                  </DialogClose>
                </DialogFooter>
              </DialogContent>
            </Dialog>
            <Sheet>
              <SheetTrigger asChild>
                <Button variant="secondary">Abrir drawer</Button>
              </SheetTrigger>
              <SheetContent>
                <SheetHeader>
                  <SheetTitle>Detalhes da tela</SheetTitle>
                  <SheetDescription>Drawer até 480 px (demonstração).</SheetDescription>
                </SheetHeader>
                <SheetBody>
                  <p className="text-sm text-text-muted">Conteúdo do painel lateral.</p>
                </SheetBody>
              </SheetContent>
            </Sheet>
            <Button variant="destructive" onClick={() => setConfirmOpen(true)}>
              <Trash2 aria-hidden="true" />
              Excluir campanha
            </Button>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="secondary">Menu</Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent>
                <DropdownMenuGroup>
                  <DropdownMenuItem>
                    <Pencil aria-hidden="true" />
                    Editar
                  </DropdownMenuItem>
                  <DropdownMenuItem>
                    <Download aria-hidden="true" />
                    Exportar
                  </DropdownMenuItem>
                </DropdownMenuGroup>
                <DropdownMenuSeparator />
                <DropdownMenuGroup>
                  <DropdownMenuItem destructive>
                    <Trash2 aria-hidden="true" />
                    Excluir
                  </DropdownMenuItem>
                </DropdownMenuGroup>
              </DropdownMenuContent>
            </DropdownMenu>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button variant="ghost" size="icon" aria-label="Ajuda">
                  <HelpCircle aria-hidden="true" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>Dica complementar (o botão tem nome acessível).</TooltipContent>
            </Tooltip>
          </div>
          <ConfirmDialog
            open={confirmOpen}
            onOpenChange={setConfirmOpen}
            destructive
            title="Excluir a campanha Verão Demonstração?"
            description="Esta ação remove o agendamento."
            consequences={['As telas deixam de exibir os anúncios desta campanha.', 'O histórico de exibições é mantido nos relatórios.']}
            requireText="Verão Demonstração"
            confirmLabel="Excluir campanha"
            onConfirm={() => toast.success('Demonstração: nada foi excluído.')}
          />
        </Card>
      </Section>

      <Section title="Abas">
        <Card>
          <Tabs defaultValue="a">
            <TabsList aria-label="Seções da tela">
              <TabsTrigger value="a">Playlist atual</TabsTrigger>
              <TabsTrigger value="b">Diagnóstico</TabsTrigger>
              <TabsTrigger value="c">Histórico</TabsTrigger>
            </TabsList>
            <TabsContent value="a">
              <p className="text-sm text-text-muted">Conteúdo da aba (demonstração).</p>
            </TabsContent>
            <TabsContent value="b">
              <p className="text-sm text-text-muted">Diagnóstico (demonstração).</p>
            </TabsContent>
            <TabsContent value="c">
              <p className="text-sm text-text-muted">Histórico (demonstração).</p>
            </TabsContent>
          </Tabs>
        </Card>
      </Section>

      <Section title="Envio de arquivos">
        <Card>
          <CardHeader>
            <div className="flex flex-col gap-1">
              <CardTitle>Biblioteca de mídia</CardTitle>
              <CardDescription>Demonstração: nenhum arquivo é enviado.</CardDescription>
            </div>
          </CardHeader>
          <FileDropzone
            accept={['image/jpeg', 'image/png', 'video/mp4']}
            maxSizeBytes={200 * 1024 * 1024}
            requirements="JPG, PNG ou MP4 (H.264) · até 200 MB · 1920 × 1080 ou 1080 × 1920 (requisitos a confirmar com o player implementado)."
            items={items}
            onFilesAccepted={(files) =>
              setItems((prev) => [
                ...prev,
                ...files.map((f, i) => ({ id: `${Date.now()}-${i}`, file: f, status: 'selected' as const })),
              ])
            }
            onRemove={(id) => setItems((prev) => prev.filter((x) => x.id !== id))}
          />
        </Card>
      </Section>
    </div>
  )
}
