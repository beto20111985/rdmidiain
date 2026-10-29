/**
 * Ordem de manutenção: dados, histórico (append-only), designar técnico, mudar status (transições do
 * backend; resolver/cancelar exige nota ≥ 10 caracteres) e anotações. POST /maintenance/{id}/assign|status|notes.
 */
import * as React from 'react'
import { useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { MessageSquarePlus, UserCog } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Textarea } from '@/components/ui/textarea'
import { Field, FieldError, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { ErrorState, NoPermission, PageSkeleton } from '@/components/patterns/states'
import { DetailList } from '@/components/ops/detail-list'
import { EstablishmentName, ScreenCode, UserName } from '@/components/ops/entity-name'
import { OptionSelect } from '@/components/ops/filter-select'
import { TechnicianSelect } from '@/components/ops/pickers'
import { ReasonDialog } from '@/components/ops/reason-dialog'
import { RealtimeStale } from '@/components/ops/realtime-stale'
import { errorMessage, isApiError } from '@/lib/errors'
import { qk } from '@/lib/queryKeys'
import { formatDateTime } from '@/lib/format'
import { maintenanceApi } from '@/pages/ops/api/client'
import { useEstablishmentLookup } from '@/pages/ops/api/hooks'
import {
  COMPONENT_LABEL,
  MAINTENANCE_EVENT_LABEL,
  MAINTENANCE_STATUS_LABEL,
  MAINTENANCE_TRANSITIONS,
  PRIORITY_LABEL,
  labelOf,
} from '@/pages/ops/api/labels'
import type { MaintenanceStatus } from '@/pages/ops/api/types'
import { maintenanceTone, priorityTone } from './maintenanceRules'

export default function MaintenanceDetailPage() {
  const { id = '' } = useParams()
  const qc = useQueryClient()
  const q = useQuery({ queryKey: qk.detail('maintenance', id), queryFn: () => maintenanceApi.get(id) })
  // horários no fuso do estabelecimento atendido (nunca o do navegador)
  const tz = useEstablishmentLookup(q.data?.establishment_id).data?.timezone ?? undefined
  const [statusOpen, setStatusOpen] = React.useState(false)
  const [newStatus, setNewStatus] = React.useState<string | null>(null)
  const [tech, setTech] = React.useState<string | null>(null)
  const [note, setNote] = React.useState('')
  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['maintenance'] })
    void qc.invalidateQueries({ queryKey: ['screens'] })
  }
  const assign = useMutation({
    mutationFn: () => maintenanceApi.assign(id, tech!),
    onSuccess: () => {
      invalidate()
      toast.success('Técnico designado e notificado.')
    },
  })
  const setStatus = useMutation({
    mutationFn: (n: string) => maintenanceApi.status(id, { status: newStatus!, note: n || null }),
    onSuccess: () => {
      invalidate()
      toast.success('Status da ordem atualizado.')
    },
  })
  const addNote = useMutation({
    mutationFn: () => maintenanceApi.note(id, note.trim()),
    onSuccess: () => {
      invalidate()
      setNote('')
      toast.success('Anotação registrada.')
    },
  })
  const o = q.data
  const crumbs = React.useMemo(() => [{ label: 'Manutenção', to: '/estoque/manutencao' }, { label: 'Ordem' }], [])

  if (q.isLoading) return <PageSkeleton />
  if (isApiError(q.error) && q.error.isForbidden) return <NoPermission permission="maintenance.manage" />
  if (q.error || !o) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />

  const transitions = MAINTENANCE_TRANSITIONS[o.status as MaintenanceStatus] ?? []
  const closing = newStatus === 'RESOLVED' || newStatus === 'CANCELLED'
  const isOpen = transitions.length > 0

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title={`Manutenção: ${labelOf(COMPONENT_LABEL, o.component)}`}
        description={`Aberta em ${formatDateTime(o.opened_at, tz)}`}
        breadcrumbs={crumbs}
        actions={
          isOpen ? (
            <Button onClick={() => { setNewStatus(null); setStatusOpen(true) }}>Mudar status</Button>
          ) : undefined
        }
      />
      <RealtimeStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} />
      <div className="grid grid-cols-1 gap-6 xl:grid-cols-12">
        <Card className="xl:col-span-7">
          <CardHeader>
            <CardTitle>Ordem</CardTitle>
            <div className="flex gap-2">
              <Badge tone={priorityTone(o.priority)}>{labelOf(PRIORITY_LABEL, o.priority)}</Badge>
              <Badge tone={maintenanceTone(o.status)}>{labelOf(MAINTENANCE_STATUS_LABEL, o.status)}</Badge>
            </div>
          </CardHeader>
          <DetailList
            items={[
              { label: 'Estabelecimento', value: <EstablishmentName id={o.establishment_id} name={o.establishment_name} /> },
              { label: 'Tela', value: o.screen_id ? <ScreenCode id={o.screen_id} code={o.screen_code} /> : null },
              { label: 'Componente', value: labelOf(COMPONENT_LABEL, o.component) },
              { label: 'Técnico', value: o.technician_user_id ? <UserName id={o.technician_user_id} name={o.technician_name} /> : 'Sem técnico' },
              { label: 'Aberta por', value: <UserName id={o.opened_by} name={o.opened_by_name} /> },
              { label: 'Encerrada em', value: formatDateTime(o.closed_at, tz) },
              { label: 'Descrição', value: o.description, wide: true },
              { label: 'Resolução', value: o.resolution, wide: true },
            ]}
          />
        </Card>
        <div className="flex flex-col gap-6 xl:col-span-5">
          {isOpen ? (
            <Card>
              <CardHeader>
                <CardTitle>Designar técnico</CardTitle>
              </CardHeader>
              <TechnicianSelect permission="maintenance.manage" value={tech} onChange={setTech} establishmentId={o.establishment_id} />
              {assign.error ? (
                <Alert variant="danger">
                  <AlertDescription className="text-text">{errorMessage(assign.error)}</AlertDescription>
                </Alert>
              ) : null}
              <Button className="w-fit" variant="secondary" onClick={() => assign.mutate()} disabled={!tech || tech === o.technician_user_id} loading={assign.isPending}>
                <UserCog aria-hidden="true" />
                Designar
              </Button>
            </Card>
          ) : null}
          <Card>
            <CardHeader>
              <CardTitle>Anotação</CardTitle>
            </CardHeader>
            <Field invalid={note.length > 0 && note.trim().length < 2}>
              <FieldLabel>Nova anotação</FieldLabel>
              <Textarea value={note} onChange={(e) => setNote(e.target.value)} rows={3} maxLength={4000} />
              <FieldError>{note.length > 0 && note.trim().length < 2 ? 'Escreva pelo menos 2 caracteres.' : null}</FieldError>
            </Field>
            {addNote.error ? (
              <Alert variant="danger">
                <AlertDescription className="text-text">{errorMessage(addNote.error)}</AlertDescription>
              </Alert>
            ) : null}
            <Button className="w-fit" variant="secondary" onClick={() => addNote.mutate()} disabled={note.trim().length < 2} loading={addNote.isPending}>
              <MessageSquarePlus aria-hidden="true" />
              Registrar anotação
            </Button>
          </Card>
        </div>
        <Card className="xl:col-span-12">
          <CardHeader>
            <CardTitle>Histórico</CardTitle>
          </CardHeader>
          <ol className="m-0 flex list-none flex-col divide-y divide-border p-0">
            {(o.history ?? []).map((e) => (
              <li key={e.id} className="flex flex-col gap-1 py-3 text-sm">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className="font-semibold text-text">
                    {labelOf(MAINTENANCE_EVENT_LABEL, e.event)}
                    {e.to_status && e.event !== 'OPENED' ? ` → ${labelOf(MAINTENANCE_STATUS_LABEL, e.to_status)}` : ''}
                  </span>
                  <time className="text-xs text-text-muted" dateTime={e.created_at ?? undefined}>
                    {formatDateTime(e.created_at, tz)}
                  </time>
                </div>
                {e.technician_user_id ? (
                  <span className="text-text-muted">
                    Técnico: <UserName id={e.technician_user_id} name={e.technician_name} />
                  </span>
                ) : null}
                {e.note ? <span className="text-text">{e.note}</span> : null}
                <span className="text-xs text-text-muted">
                  Por <UserName id={e.actor_user_id} name={e.actor_name} />
                </span>
              </li>
            ))}
          </ol>
        </Card>
      </div>
      <ReasonDialog
        open={statusOpen}
        onOpenChange={setStatusOpen}
        title="Mudar o status da ordem"
        confirmLabel="Salvar status"
        minReason={closing ? 10 : 0}
        reasonLabel={closing ? (newStatus === 'RESOLVED' ? 'Resolução' : 'Motivo do cancelamento') : 'Observação (opcional)'}
        reasonHint={closing ? 'Obrigatório ao resolver ou cancelar (mínimo 10 caracteres).' : 'Fica no histórico da ordem.'}
        extraValid={!!newStatus}
        onConfirm={(n) => setStatus.mutateAsync(n)}
      >
        <Field required>
          <FieldLabel>Novo status</FieldLabel>
          <OptionSelect value={newStatus} onChange={setNewStatus} options={transitions.map((t) => ({ value: t, label: MAINTENANCE_STATUS_LABEL[t] }))} />
        </Field>
      </ReasonDialog>
    </div>
  )
}
