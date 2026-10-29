/**
 * Instalações (técnico): agendadas, em andamento e concluídas; agendar instalação com técnico
 * designado. GET/POST /installations (equipment.view / equipment.manage), escopo por cidade.
 */
import * as React from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { CalendarPlus, HardHat } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Can } from '@/components/auth/guards'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Field, FieldLabel } from '@/components/ui/field'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { NoPermission } from '@/components/patterns/states'
import { EstablishmentName, ScreenCode, UserName } from '@/components/ops/entity-name'
import { FilterSelect } from '@/components/ops/filter-select'
import { EstablishmentPicker, ScreenPicker, TechnicianSelect } from '@/components/ops/pickers'
import { RealtimeStale } from '@/components/ops/realtime-stale'
import { useTableState } from '@/hooks/useTableState'
import { errorMessage, isApiError } from '@/lib/errors'
import { qk } from '@/lib/queryKeys'
import { formatDate } from '@/lib/format'
import { installationsApi } from '@/pages/ops/api/client'
import { INSTALLATION_STATUS_LABEL, labelOf } from '@/pages/ops/api/labels'
import type { Installation } from '@/pages/ops/api/types'
import { installationTone } from './installationRules'

export default function InstallationsListPage() {
  const table = useTableState({ defaultPageSize: 25, filterKeys: ['status', 'mine'] })
  const f = table.state.filters
  const params = { page: table.state.page, page_size: table.state.pageSize, status: f.status, mine: f.mine }
  const q = useQuery({
    queryKey: qk.sub('maintenance', 'installations', 'list', params),
    queryFn: () => installationsApi.list(params),
    placeholderData: keepPreviousData,
  })
  const [open, setOpen] = React.useState(false)
  if (isApiError(q.error) && q.error.isForbidden) return <NoPermission permission="equipment.view" />

  const columns: DataTableColumn<Installation>[] = [
    {
      id: 'est',
      header: 'Instalação',
      mobile: 'primary',
      cell: (i) => (
        <Link to={`/estoque/instalacoes/${i.id}`} className="font-semibold text-link underline-offset-4 hover:underline">
          <EstablishmentName id={i.establishment_id} name={i.establishment_name} link={false} />
        </Link>
      ),
    },
    { id: 'screen', header: 'Tela', mobile: 'secondary', cell: (i) => (i.screen_id ? <ScreenCode id={i.screen_id} code={i.screen_code} /> : 'Tela a definir') },
    { id: 'tech', header: 'Técnico', cell: (i) => <UserName id={i.technician_user_id} name={i.technician_name} /> },
    { id: 'date', header: 'Data agendada', cell: (i) => (i.scheduled_date ? formatDate(`${i.scheduled_date}T12:00:00Z`) : 'A definir') },
    { id: 'status', header: 'Status', mobile: 'badge', cell: (i) => <Badge tone={installationTone(i.status)}>{labelOf(INSTALLATION_STATUS_LABEL, i.status)}</Badge> },
  ]

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Instalações"
        description="Checklist, GPS, fotos, assinatura do responsável e teste de conexão real do player."
        actions={
          <Can permission="equipment.manage">
            <Button onClick={() => setOpen(true)}>
              <CalendarPlus aria-hidden="true" />
              Agendar instalação
            </Button>
          </Can>
        }
      />
      <RealtimeStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />
      <Card>
        <DataTable
          caption="Instalações"
          columns={columns}
          data={q.data}
          getRowId={(i) => i.id}
          controller={table}
          isLoading={q.isLoading}
          isFetching={q.isFetching}
          error={q.error}
          onRetry={() => void q.refetch()}
          filtersActive={Object.keys(f).length > 0}
          empty={{ icon: HardHat, title: 'Nenhuma instalação.', description: 'Agende a instalação designando o técnico responsável.' }}
          toolbar={
            <>
              <FilterSelect label="Status" value={f.status} onChange={(v) => table.setFilter('status', v)} options={Object.entries(INSTALLATION_STATUS_LABEL).map(([value, label]) => ({ value, label }))} />
              <FilterSelect label="Responsável" value={f.mine} onChange={(v) => table.setFilter('mine', v)} options={[{ value: 'true', label: 'Minhas instalações' }]} allLabel="Qualquer técnico" />
            </>
          }
        />
      </Card>
      <ScheduleDialog open={open} onOpenChange={setOpen} />
    </div>
  )
}

function ScheduleDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [estId, setEstId] = React.useState<string | null>(null)
  const [screenId, setScreenId] = React.useState<string | null>(null)
  const [tech, setTech] = React.useState<string | null>(null)
  const [date, setDate] = React.useState('')
  const [touched, setTouched] = React.useState(false)
  React.useEffect(() => {
    if (open) {
      setEstId(null)
      setScreenId(null)
      setTech(null)
      setDate('')
      setTouched(false)
    }
  }, [open])
  const errs = { est: !estId ? 'Selecione o estabelecimento.' : null, tech: !tech ? 'Selecione o técnico.' : null }
  const m = useMutation({
    mutationFn: () => installationsApi.create({ establishment_id: estId, screen_id: screenId, technician_user_id: tech, scheduled_date: date || null }),
    onSuccess: (i) => {
      void qc.invalidateQueries({ queryKey: ['maintenance'] })
      toast.success('Instalação agendada. O técnico foi notificado.')
      onOpenChange(false)
      navigate(`/estoque/instalacoes/${i.id}`)
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
            if (!errs.est && !errs.tech) m.mutate()
          }}
        >
          <DialogHeader>
            <DialogTitle>Agendar instalação</DialogTitle>
            <DialogDescription>O checklist padrão é criado automaticamente.</DialogDescription>
          </DialogHeader>
          <EstablishmentPicker
            value={estId}
            onChange={(id) => {
              setEstId(id)
              setScreenId(null)
              setTech(null)
            }}
            required
            onlyActive
            error={touched ? errs.est : null}
          />
          {estId ? (
            <ScreenPicker
              value={screenId}
              onChange={setScreenId}
              establishmentId={estId}
              legend="Tela (pode ser vinculada depois)"
              description="A conclusão exige a tela vinculada; se ela ainda não existe, vincule depois em “Editar agendamento” na instalação."
            />
          ) : null}
          <TechnicianSelect
            permission="equipment.manage"
            value={tech}
            onChange={setTech}
            required
            error={touched ? errs.tech : null}
            establishmentId={estId}
            disabled={!estId}
          />
          <Field>
            <FieldLabel>Data agendada</FieldLabel>
            <Input type="date" value={date} onChange={(e) => setDate(e.target.value)} />
          </Field>
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
              Agendar
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
