/**
 * Ordens de manutenção (técnico/RD): status, técnico, prioridade e histórico. Abrir ordem pela tela
 * ou estabelecimento (opcionalmente coloca a tela em manutenção — continua no inventário).
 * GET/POST /maintenance (W-ORG).
 */
import * as React from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Plus, Wrench } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { NoPermission } from '@/components/patterns/states'
import { EstablishmentName, ScreenCode, UserName } from '@/components/ops/entity-name'
import { FilterSelect, OptionSelect } from '@/components/ops/filter-select'
import { EstablishmentPicker, ScreenPicker, TechnicianSelect } from '@/components/ops/pickers'
import { RealtimeStale } from '@/components/ops/realtime-stale'
import { useTableState } from '@/hooks/useTableState'
import { errorMessage, isApiError } from '@/lib/errors'
import { qk } from '@/lib/queryKeys'
import { formatDateTime } from '@/lib/format'
import { maintenanceApi } from '@/pages/ops/api/client'
import { COMPONENT_LABEL, MAINTENANCE_STATUS_LABEL, PRIORITY_LABEL, labelOf } from '@/pages/ops/api/labels'
import type { MaintenanceOrder } from '@/pages/ops/api/types'
import { maintenanceTone, priorityTone } from './maintenanceRules'

export default function MaintenanceListPage() {
  const table = useTableState({ defaultPageSize: 25, filterKeys: ['status', 'open_only', 'mine'] })
  const f = table.state.filters
  const params = { page: table.state.page, page_size: table.state.pageSize, status: f.status, open_only: f.open_only, mine: f.mine }
  const q = useQuery({ queryKey: qk.list('maintenance', params), queryFn: () => maintenanceApi.list(params), placeholderData: keepPreviousData })
  const [open, setOpen] = React.useState(false)

  if (isApiError(q.error) && q.error.isForbidden) return <NoPermission permission="maintenance.manage" />

  const columns: DataTableColumn<MaintenanceOrder>[] = [
    {
      id: 'target',
      header: 'Ordem',
      mobile: 'primary',
      cell: (o) => (
        <div className="flex flex-col">
          <Link to={`/estoque/manutencao/${o.id}`} className="font-semibold text-link underline-offset-4 hover:underline">
            {labelOf(COMPONENT_LABEL, o.component)} — {o.description.length > 60 ? `${o.description.slice(0, 60)}…` : o.description}
          </Link>
        </div>
      ),
    },
    { id: 'est', header: 'Estabelecimento', mobile: 'secondary', cell: (o) => <EstablishmentName id={o.establishment_id} name={o.establishment_name} link={false} /> },
    { id: 'screen', header: 'Tela', cell: (o) => (o.screen_id ? <ScreenCode id={o.screen_id} code={o.screen_code} /> : '—') },
    { id: 'priority', header: 'Prioridade', cell: (o) => <Badge tone={priorityTone(o.priority)}>{labelOf(PRIORITY_LABEL, o.priority)}</Badge> },
    { id: 'status', header: 'Status', mobile: 'badge', cell: (o) => <Badge tone={maintenanceTone(o.status)}>{labelOf(MAINTENANCE_STATUS_LABEL, o.status)}</Badge> },
    { id: 'tech', header: 'Técnico', cell: (o) => (o.technician_user_id ? <UserName id={o.technician_user_id} name={o.technician_name} /> : 'Sem técnico') },
    { id: 'opened', header: 'Aberta em', mobile: 'hidden', cell: (o) => formatDateTime(o.opened_at) },
  ]

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Manutenção"
        description="Ordens de manutenção e visitas técnicas. Tela em manutenção continua vendável."
        actions={
          <Button onClick={() => setOpen(true)}>
            <Plus aria-hidden="true" />
            Abrir ordem
          </Button>
        }
      />
      <RealtimeStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />
      <Card>
        <DataTable
          caption="Ordens de manutenção"
          columns={columns}
          data={q.data}
          getRowId={(o) => o.id}
          controller={table}
          isLoading={q.isLoading}
          isFetching={q.isFetching}
          error={q.error}
          onRetry={() => void q.refetch()}
          filtersActive={Object.keys(f).length > 0}
          empty={{ icon: Wrench, title: 'Nenhuma ordem de manutenção.', description: 'Abra uma ordem quando uma TV, TV Box, cabo ou rede precisar de atendimento.' }}
          toolbar={
            <>
              <FilterSelect label="Status" value={f.status} onChange={(v) => table.setFilter('status', v)} options={Object.entries(MAINTENANCE_STATUS_LABEL).map(([value, label]) => ({ value, label }))} />
              <FilterSelect
                label="Situação"
                value={f.open_only}
                onChange={(v) => table.setFilter('open_only', v)}
                options={[{ value: 'true', label: 'Somente em aberto' }]}
                allLabel="Todas"
              />
              <FilterSelect label="Responsável" value={f.mine} onChange={(v) => table.setFilter('mine', v)} options={[{ value: 'true', label: 'Minhas ordens' }]} allLabel="Qualquer técnico" />
            </>
          }
        />
      </Card>
      <OpenOrderDialog open={open} onOpenChange={setOpen} />
    </div>
  )
}

function OpenOrderDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [by, setBy] = React.useState<'screen' | 'establishment'>('screen')
  const [screenId, setScreenId] = React.useState<string | null>(null)
  const [screenEstId, setScreenEstId] = React.useState<string | null>(null)
  const [estId, setEstId] = React.useState<string | null>(null)
  const [component, setComponent] = React.useState<string | null>('TV')
  const [priority, setPriority] = React.useState<string | null>('NORMAL')
  const [desc, setDesc] = React.useState('')
  const [tech, setTech] = React.useState<string | null>(null)
  const [putMaint, setPutMaint] = React.useState(true)
  const [touched, setTouched] = React.useState(false)
  React.useEffect(() => {
    if (open) {
      setBy('screen')
      setScreenId(null)
      setScreenEstId(null)
      setEstId(null)
      setComponent('TV')
      setPriority('NORMAL')
      setDesc('')
      setTech(null)
      setPutMaint(true)
      setTouched(false)
    }
  }, [open])
  const errs = {
    target: by === 'screen' ? (!screenId ? 'Selecione a tela.' : null) : !estId ? 'Selecione o estabelecimento.' : null,
    desc: desc.trim().length < 10 ? 'Descreva o problema (mínimo 10 caracteres).' : null,
  }
  const m = useMutation({
    mutationFn: () =>
      maintenanceApi.open({
        screen_id: by === 'screen' ? screenId : null,
        establishment_id: by === 'establishment' ? estId : null,
        component,
        priority,
        description: desc.trim(),
        technician_user_id: tech,
        put_screen_in_maintenance: by === 'screen' ? putMaint : false,
      }),
    onSuccess: (o) => {
      void qc.invalidateQueries({ queryKey: ['maintenance'] })
      void qc.invalidateQueries({ queryKey: ['screens'] })
      toast.success('Ordem de manutenção aberta.')
      onOpenChange(false)
      navigate(`/estoque/manutencao/${o.id}`)
    },
  })
  return (
    <Dialog open={open} onOpenChange={(o) => (!m.isPending ? onOpenChange(o) : undefined)}>
      <DialogContent size="md">
        <form
          noValidate
          className="flex flex-col gap-5"
          onSubmit={(e) => {
            e.preventDefault()
            setTouched(true)
            if (!errs.target && !errs.desc) m.mutate()
          }}
        >
          <DialogHeader>
            <DialogTitle>Abrir ordem de manutenção</DialogTitle>
            <DialogDescription>O técnico designado é notificado.</DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-2">
            <span id="mo-by" className="text-sm font-semibold text-text">
              Atendimento para
            </span>
            <ToggleGroup type="single" value={by} onValueChange={(v) => v && setBy(v as typeof by)} aria-labelledby="mo-by">
              <ToggleGroupItem value="screen">Uma tela</ToggleGroupItem>
              <ToggleGroupItem value="establishment">O estabelecimento</ToggleGroupItem>
            </ToggleGroup>
          </div>
          {by === 'screen' ? (
            <ScreenPicker
              value={screenId}
              onChange={(id, s) => {
                setScreenId(id)
                setScreenEstId(s?.establishment_id ?? null)
                setTech(null)
              }}
              required
              error={touched ? errs.target : null}
            />
          ) : (
            <EstablishmentPicker
              value={estId}
              onChange={(id) => {
                setEstId(id)
                setTech(null)
              }}
              required
              error={touched ? errs.target : null}
            />
          )}
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <Field required>
              <FieldLabel>Componente</FieldLabel>
              <OptionSelect value={component} onChange={setComponent} options={Object.entries(COMPONENT_LABEL).map(([value, label]) => ({ value, label }))} />
            </Field>
            <Field required>
              <FieldLabel>Prioridade</FieldLabel>
              <OptionSelect value={priority} onChange={setPriority} options={Object.entries(PRIORITY_LABEL).map(([value, label]) => ({ value, label }))} />
            </Field>
          </div>
          <Field invalid={touched && !!errs.desc} required>
            <FieldLabel>Descrição do problema</FieldLabel>
            <Textarea value={desc} onChange={(e) => setDesc(e.target.value)} rows={3} maxLength={4000} />
            <FieldError>{touched ? errs.desc : null}</FieldError>
          </Field>
          <TechnicianSelect
            permission="maintenance.manage"
            value={tech}
            onChange={setTech}
            label="Técnico (opcional)"
            establishmentId={by === 'screen' ? screenEstId : estId}
            disabled={by === 'screen' ? !screenEstId : !estId}
          />
          {by === 'screen' ? (
            <Field orientation="horizontal" className="min-h-11">
              <Switch checked={putMaint} onCheckedChange={setPutMaint} />
              <FieldLabel>Colocar a tela em manutenção</FieldLabel>
              <FieldDescription className="w-full">A tela continua no inventário; volta a “Ativa” quando todas as ordens forem resolvidas.</FieldDescription>
            </Field>
          ) : null}
          {m.error ? (
            <Alert variant="danger">
              <AlertDescription className="text-text">{errorMessage(m.error)}</AlertDescription>
            </Alert>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)} disabled={m.isPending}>
              Cancelar
            </Button>
            <Button type="submit" loading={m.isPending}>
              Abrir ordem
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
