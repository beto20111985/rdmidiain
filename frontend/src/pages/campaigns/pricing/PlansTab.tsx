/**
 * Planos (REQ-PLN-001/004/009–011): pré-definidos (ativar/desativar/duplicar) e personalizados
 * por cliente (aprovação por OUTRA pessoa com `pricing.manage`). A margem é calculada pelo
 * servidor ao ativar: negativa bloqueia; abaixo de 40% gera alerta.
 */
import * as React from 'react'
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Plus } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Checkbox } from '@/components/ui/checkbox'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Skeleton } from '@/components/ui/skeleton'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { ReasonDialog } from '@/components/campaigns/reason-dialog'
import { ToneBadge } from '@/components/campaigns/badges'
import { QuoteBreakdown } from '@/components/campaigns/quote-breakdown'
import { EntityPicker, type PickerOption } from '@/components/campaigns/entity-picker'
import { OrgName } from '@/components/campaigns/org-name'
import { FilterSelect } from '@/components/campaigns/filter-select'
import { useTableState } from '@/hooks/useTableState'
import { qk } from '@/lib/queryKeys'
import { isApiError } from '@/lib/errors'
import { formatBps, formatCents } from '@/lib/format'
import { pricingApi } from '../lib/api'
import { FREQ_LABEL, PERIOD_LABEL, PLAN_STATUS, WEEKDAYS } from '../lib/labels'
import { ALLOWED_DURATIONS, maskFromDays } from '../lib/rules'
import { parsePercentToBps } from '../lib/money'
import { searchOrganizations } from '../lib/pickers'
import type { FreqKind, PeriodType, Plan, PlanStatus } from '../lib/types'
import { describeError } from '@/pages/campaigns/lib/errorText'

const MARGIN_TONE: Record<string, { label: string; tone: 'success' | 'warning' | 'danger' }> = {
  OK: { label: 'Margem OK', tone: 'success' },
  BELOW_MIN: { label: 'Margem abaixo de 40%', tone: 'warning' },
  NEGATIVE: { label: 'Margem negativa', tone: 'danger' },
}

export function PlansTab({ canManage }: { canManage: boolean }) {
  const qc = useQueryClient()
  const table = useTableState({ defaultPageSize: 25, prefix: 'pl_', filterKeys: ['status'] })
  const params = { page: table.state.page, page_size: table.state.pageSize, status: table.state.filters.status }
  const q = useQuery({ queryKey: qk.list('pricing', { plans: params }), queryFn: () => pricingApi.plans(params), placeholderData: keepPreviousData })
  const [creating, setCreating] = React.useState(false)
  const [rejecting, setRejecting] = React.useState<Plan | null>(null)
  const [marginOf, setMarginOf] = React.useState<Plan | null>(null)
  const refresh = () => void qc.invalidateQueries({ queryKey: qk.root('pricing') })

  const run = async (fn: () => Promise<Plan>, ok: string) => {
    try {
      const p = await fn()
      if (p.warnings?.length) toast.warning(`${ok} Alerta: margem ${formatBps(p.warnings[0].margin_bps ?? null)} abaixo do mínimo.`)
      else toast.success(ok)
      refresh()
    } catch (e) {
      if (isApiError(e) && e.code === 'PLAN_MARGIN_NEGATIVE') toast.error(`Plano bloqueado: margem negativa (${formatCents(e.details.margin_cents as number)}).`)
      else toast.error(describeError(e))
    }
  }

  const cols: DataTableColumn<Plan>[] = [
    {
      id: 'name',
      header: 'Plano',
      mobile: 'primary',
      cell: (p) => (
        <span className="flex flex-col">
          <span className="font-semibold">{p.name}</span>
          <span className="text-xs text-text-muted">{p.kind === 'CUSTOM' ? <>Personalizado · <OrgName id={p.org_id} /></> : 'Pré-definido'}</span>
        </span>
      ),
    },
    { id: 'status', header: 'Status', mobile: 'badge', cell: (p) => <ToneBadge {...PLAN_STATUS[p.status]} /> },
    { id: 'spec', header: 'Configuração', cell: (p) => `${PERIOD_LABEL[p.period_type]} · ${p.duration_s} s · ${p.freq_value} ${FREQ_LABEL[p.freq_kind].toLowerCase()} · ${p.screens_min}–${p.screens_max} telas` },
    { id: 'discount', header: 'Desconto', numeric: true, cell: (p) => formatBps(p.discount_bps, 0) },
    {
      id: 'margin',
      header: 'Margem',
      cell: (p) => (p.margin_status ? <ToneBadge {...(MARGIN_TONE[p.margin_status] ?? { label: p.margin_status, tone: 'warning' })} /> : '—'),
    },
  ]

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-title-section">Planos</h2>
        {canManage ? (
          <Button onClick={() => setCreating(true)}>
            <Plus aria-hidden="true" />
            Novo plano
          </Button>
        ) : null}
      </div>
      <DataTable
        caption="Planos"
        columns={cols}
        data={q.data}
        getRowId={(p) => p.id}
        controller={table}
        isLoading={q.isLoading}
        isFetching={q.isFetching}
        error={q.error}
        onRetry={() => void q.refetch()}
        filtersActive={!!table.state.filters.status}
        empty={{ title: 'Nenhum plano cadastrado', description: 'Planos pré-definidos aparecem para os anunciantes na criação de campanha.' }}
        toolbar={
          <FilterSelect
            label="Status"
            value={table.state.filters.status}
            onChange={(v) => table.setFilter('status', v)}
            options={(Object.keys(PLAN_STATUS) as PlanStatus[]).map((s) => ({ value: s, label: PLAN_STATUS[s].label }))}
          />
        }
        rowActions={(p) => (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="secondary" aria-label={`Ações do plano ${p.name}`}>
                Ações
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuGroup>
                <DropdownMenuItem onSelect={() => setMarginOf(p)}>Ver margem</DropdownMenuItem>
                {canManage && (p.status === 'RASCUNHO' || p.status === 'INATIVO') ? (
                  <DropdownMenuItem onSelect={() => void run(() => pricingApi.activatePlan(p.id), 'Plano ativado.')}>Ativar</DropdownMenuItem>
                ) : null}
                {canManage && p.status === 'ATIVO' ? (
                  <DropdownMenuItem onSelect={() => void run(() => pricingApi.deactivatePlan(p.id), 'Plano desativado.')}>Desativar</DropdownMenuItem>
                ) : null}
                {canManage ? <DropdownMenuItem onSelect={() => void run(() => pricingApi.duplicatePlan(p.id), 'Plano duplicado.')}>Duplicar</DropdownMenuItem> : null}
                {canManage && p.status === 'PENDENTE_APROVACAO' ? (
                  <>
                    <DropdownMenuItem onSelect={() => void run(() => pricingApi.approvePlan(p.id), 'Plano aprovado e ativado.')}>Aprovar</DropdownMenuItem>
                    <DropdownMenuItem onSelect={() => setRejecting(p)}>Rejeitar</DropdownMenuItem>
                  </>
                ) : null}
              </DropdownMenuGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      />
      <p className="m-0 text-xs text-text-muted">Plano personalizado: quem cria não pode aprovar (o servidor recusa a autoaprovação).</p>
      <PlanDialog open={creating} onOpenChange={setCreating} onSaved={refresh} />
      <ReasonDialog
        open={!!rejecting}
        onOpenChange={(o) => !o && setRejecting(null)}
        destructive
        title={`Rejeitar o plano ${rejecting?.name ?? ''}?`}
        minLength={10}
        confirmLabel="Rejeitar plano"
        onConfirm={async (reason) => {
          await pricingApi.rejectPlan(rejecting!.id, reason)
          toast.success('Plano rejeitado.')
          refresh()
        }}
      />
      <MarginDialog plan={marginOf} onClose={() => setMarginOf(null)} />
    </div>
  )
}

function MarginDialog({ plan, onClose }: { plan: Plan | null; onClose: () => void }) {
  const q = useQuery({ queryKey: qk.sub('pricing', 'plan-margin', plan?.id ?? '-'), queryFn: () => pricingApi.planMargin(plan!.id), enabled: !!plan, retry: false })
  if (!plan) return null
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent size="md" className="max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Margem do plano {plan.name}</DialogTitle>
          <DialogDescription>Cotação de referência calculada pelo servidor com a tabela de preços ativa.</DialogDescription>
        </DialogHeader>
        {q.isLoading ? <Skeleton className="h-48 w-full" /> : q.error ? <Alert variant="danger"><AlertDescription>{describeError(q.error)}</AlertDescription></Alert> : q.data ? <QuoteBreakdown quote={q.data.quote} showGroups={false} title="Cotação de referência (servidor)" /> : null}
        <DialogFooter>
          <Button variant="secondary" onClick={onClose}>
            Fechar
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function PlanDialog({ open, onOpenChange, onSaved }: { open: boolean; onOpenChange: (o: boolean) => void; onSaved: () => void }) {
  const [f, setF] = React.useState({
    name: '',
    description: '',
    kind: 'PREDEFINED' as 'PREDEFINED' | 'CUSTOM',
    period_type: 'MENSAL' as PeriodType,
    period_days: '',
    duration_s: 30,
    freq_kind: 'PER_HOUR' as FreqKind,
    freq_value: '1',
    days: [0, 1, 2, 3, 4, 5, 6],
    screens_min: '1',
    screens_max: '1',
    priority: 'STANDARD',
    exclusivity: 'NONE',
    discount: '0',
    max_campaigns: '',
    max_creatives: '',
    max_screens: '',
    max_upload_mb: '',
  })
  const [org, setOrg] = React.useState<PickerOption | null>(null)
  const [errors, setErrors] = React.useState<Record<string, string>>({})
  const [error, setError] = React.useState<unknown>(null)
  const [pending, setPending] = React.useState(false)
  React.useEffect(() => {
    if (open) {
      setErrors({})
      setError(null)
    }
  }, [open])
  const set = (patch: Partial<typeof f>) => setF((x) => ({ ...x, ...patch }))
  const optInt = (s: string) => (s.trim() === '' ? null : Math.floor(Number(s)))

  const save = async () => {
    const e: Record<string, string> = {}
    if (f.name.trim().length < 3) e.name = 'Mínimo de 3 caracteres.'
    if (f.kind === 'CUSTOM' && !org) e.org = 'Plano personalizado exige o cliente.'
    if (f.period_type === 'AVULSO' && !(Number(f.period_days) >= 1)) e.period_days = 'Informe a quantidade de dias.'
    const smin = Number(f.screens_min)
    const smax = Number(f.screens_max)
    if (!(smin >= 1) || !(smax >= smin)) e.screens = 'Faixa de telas inválida (máximo ≥ mínimo ≥ 1).'
    const disc = parsePercentToBps(f.discount)
    if (disc === null || disc < 0 || disc > 10000) e.discount = 'Desconto entre 0 e 100%.'
    if (!f.days.length) e.days = 'Selecione ao menos um dia.'
    setErrors(e)
    if (Object.keys(e).length) return
    setPending(true)
    setError(null)
    try {
      await pricingApi.createPlan({
        name: f.name.trim(),
        description: f.description.trim() || null,
        kind: f.kind,
        org_id: f.kind === 'CUSTOM' ? org!.id : null,
        period_type: f.period_type,
        period_days: f.period_type === 'AVULSO' ? Number(f.period_days) : null,
        duration_s: f.duration_s,
        freq_kind: f.freq_kind === 'SPECIFIC_TIME' ? 'PER_HOUR' : f.freq_kind,
        freq_value: Math.max(1, Math.floor(Number(f.freq_value) || 1)),
        days_mask: maskFromDays(f.days),
        time_bands: [[0, 1440]],
        screens_min: smin,
        screens_max: smax,
        priority: f.priority,
        exclusivity: f.exclusivity,
        discount_bps: disc,
        max_campaigns: optInt(f.max_campaigns),
        max_creatives: optInt(f.max_creatives),
        max_screens: optInt(f.max_screens),
        max_upload_mb: optInt(f.max_upload_mb),
      })
      toast.success(f.kind === 'CUSTOM' ? 'Plano criado — aguardando aprovação de outra pessoa.' : 'Plano criado como rascunho.')
      onSaved()
      onOpenChange(false)
    } catch (err) {
      setError(err)
    } finally {
      setPending(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !pending && onOpenChange(o)}>
      <DialogContent size="md" className="max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Novo plano</DialogTitle>
          <DialogDescription>O preço do plano vem da tabela ativa; aqui definem-se configuração, desconto e limites.</DialogDescription>
        </DialogHeader>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Field invalid={!!errors.name} className="md:col-span-2">
            <FieldLabel required>Nome</FieldLabel>
            <Input value={f.name} onChange={(e) => set({ name: e.target.value })} maxLength={120} />
            <FieldError>{errors.name}</FieldError>
          </Field>
          <Field className="md:col-span-2">
            <FieldLabel>Descrição</FieldLabel>
            <Textarea value={f.description} onChange={(e) => set({ description: e.target.value })} rows={2} />
          </Field>
          <Field>
            <FieldLabel>Tipo</FieldLabel>
            <Select value={f.kind} onValueChange={(v) => set({ kind: v as 'PREDEFINED' | 'CUSTOM' })}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  <SelectItem value="PREDEFINED">Pré-definido (catálogo)</SelectItem>
                  <SelectItem value="CUSTOM">Personalizado (um cliente)</SelectItem>
                </SelectGroup>
              </SelectContent>
            </Select>
          </Field>
          {f.kind === 'CUSTOM' ? (
            <EntityPicker label="Cliente" domain="organizations" value={org} onChange={setOrg} error={errors.org} required search={(t) => searchOrganizations(t, 'ADVERTISER')} />
          ) : (
            <div />
          )}
          <Field>
            <FieldLabel>Período</FieldLabel>
            <Select value={f.period_type} onValueChange={(v) => set({ period_type: v as PeriodType })}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  {(Object.keys(PERIOD_LABEL) as PeriodType[]).map((p) => (
                    <SelectItem key={p} value={p}>
                      {PERIOD_LABEL[p]}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
            <FieldDescription>Sem trimestral.</FieldDescription>
          </Field>
          {f.period_type === 'AVULSO' ? (
            <Field invalid={!!errors.period_days}>
              <FieldLabel required>Dias</FieldLabel>
              <Input type="number" min={1} max={366} value={f.period_days} onChange={(e) => set({ period_days: e.target.value })} />
              <FieldError>{errors.period_days}</FieldError>
            </Field>
          ) : (
            <div />
          )}
          <div className="flex flex-col gap-2">
            <span id="plan-duration" className="text-sm font-semibold">
              Duração
            </span>
            <ToggleGroup type="single" value={String(f.duration_s)} onValueChange={(v) => v && set({ duration_s: Number(v) })} aria-labelledby="plan-duration">
              {ALLOWED_DURATIONS.map((d) => (
                <ToggleGroupItem key={d} value={String(d)}>
                  {d} s
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Field>
              <FieldLabel>Frequência</FieldLabel>
              <Select value={f.freq_kind} onValueChange={(v) => set({ freq_kind: v as FreqKind })}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    <SelectItem value="PER_HOUR">{FREQ_LABEL.PER_HOUR}</SelectItem>
                    <SelectItem value="PER_DAY">{FREQ_LABEL.PER_DAY}</SelectItem>
                  </SelectGroup>
                </SelectContent>
              </Select>
            </Field>
            <Field>
              <FieldLabel>Inserções</FieldLabel>
              <Input type="number" min={1} max={1440} value={f.freq_value} onChange={(e) => set({ freq_value: e.target.value })} />
            </Field>
          </div>
          <fieldset className="m-0 flex flex-col gap-1 border-0 p-0 md:col-span-2">
            <legend className="mb-1 text-sm font-semibold">Dias da semana</legend>
            <div className="flex flex-wrap gap-x-4">
              {WEEKDAYS.map((d) => (
                <Field key={d.bit} orientation="horizontal" className="min-h-11 w-auto">
                  <Checkbox
                    checked={f.days.includes(d.bit)}
                    onCheckedChange={(c) => set({ days: c ? [...f.days, d.bit] : f.days.filter((b) => b !== d.bit) })}
                  />
                  <FieldLabel className="font-normal">{d.short}</FieldLabel>
                </Field>
              ))}
            </div>
            {errors.days ? <FieldError>{errors.days}</FieldError> : null}
          </fieldset>
          <Field invalid={!!errors.screens}>
            <FieldLabel>Telas (mínimo)</FieldLabel>
            <Input type="number" min={1} value={f.screens_min} onChange={(e) => set({ screens_min: e.target.value })} />
            <FieldError>{errors.screens}</FieldError>
          </Field>
          <Field>
            <FieldLabel>Telas (máximo)</FieldLabel>
            <Input type="number" min={1} value={f.screens_max} onChange={(e) => set({ screens_max: e.target.value })} />
          </Field>
          <Field>
            <FieldLabel>Prioridade</FieldLabel>
            <Select value={f.priority} onValueChange={(v) => set({ priority: v })}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  <SelectItem value="STANDARD">Padrão</SelectItem>
                  <SelectItem value="PREMIUM">Premium</SelectItem>
                </SelectGroup>
              </SelectContent>
            </Select>
          </Field>
          <Field>
            <FieldLabel>Exclusividade</FieldLabel>
            <Select value={f.exclusivity} onValueChange={(v) => set({ exclusivity: v })}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  <SelectItem value="NONE">Sem exclusividade</SelectItem>
                  <SelectItem value="COMPETITOR_EXCLUSIVE">Exclusividade de concorrente</SelectItem>
                </SelectGroup>
              </SelectContent>
            </Select>
          </Field>
          <Field invalid={!!errors.discount}>
            <FieldLabel>Desconto do plano (%)</FieldLabel>
            <Input value={f.discount} onChange={(e) => set({ discount: e.target.value })} inputMode="decimal" />
            <FieldError>{errors.discount}</FieldError>
          </Field>
          <Field>
            <FieldLabel>Máx. de campanhas</FieldLabel>
            <Input type="number" min={0} value={f.max_campaigns} onChange={(e) => set({ max_campaigns: e.target.value })} />
          </Field>
          <Field>
            <FieldLabel>Máx. de anúncios</FieldLabel>
            <Input type="number" min={0} value={f.max_creatives} onChange={(e) => set({ max_creatives: e.target.value })} />
          </Field>
          <Field>
            <FieldLabel>Máx. de telas</FieldLabel>
            <Input type="number" min={0} value={f.max_screens} onChange={(e) => set({ max_screens: e.target.value })} />
          </Field>
          <Field>
            <FieldLabel>Limite de upload (MB)</FieldLabel>
            <Input type="number" min={0} value={f.max_upload_mb} onChange={(e) => set({ max_upload_mb: e.target.value })} />
          </Field>
        </div>
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
            Criar plano
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
