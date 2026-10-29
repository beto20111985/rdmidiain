/**
 * Detalhe da tela (RD): presença em tempo real, dispositivo e telemetria, comandos remotos,
 * programação (âncora, grade prevista, manifesto), hardware/troca, audiência ESTIMADA e histórico.
 * Ações: editar, mudar status, mover de estabelecimento, retirar definitivamente (código digitado).
 */
import * as React from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { ArrowRightLeft, Ban, Pencil, QrCode, ToggleLeft } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Can } from '@/components/auth/guards'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Field, FieldLabel } from '@/components/ui/field'
import { ErrorState, NoPermission, PageSkeleton } from '@/components/patterns/states'
import { AudiencePanel } from '@/components/ops/audience-panel'
import { OptionSelect } from '@/components/ops/filter-select'
import { EstablishmentPicker } from '@/components/ops/pickers'
import { ReasonDialog } from '@/components/ops/reason-dialog'
import { RealtimeStale } from '@/components/ops/realtime-stale'
import { PresenceBadge } from '@/components/ops/presence-badge'
import { useCan } from '@/hooks/usePermissions'
import { isApiError } from '@/lib/errors'
import { qk } from '@/lib/queryKeys'
import { formatDateTime } from '@/lib/format'
import { screensApi, presenceToSummary } from '@/pages/ops/api/client'
import { useScreenPresence } from '@/pages/ops/api/hooks'
import { SCREEN_STATUS_LABEL, labelOf } from '@/pages/ops/api/labels'
import { screenStatusTone } from './screenColumns'
import { lowerFirst, screenInventoryReason } from './inventoryStatus'
import { ScreenStatusTab } from './detail/ScreenStatusTab'
import { ScreenCommandsTab } from './detail/ScreenCommandsTab'
import { ScreenScheduleTab } from './detail/ScreenScheduleTab'
import { ScreenHardwareTab } from './detail/ScreenHardwareTab'
import { ScreenHistoryTab } from './detail/ScreenHistoryTab'

const TABS = ['status', 'comandos', 'programacao', 'hardware', 'audiencia', 'historico'] as const
type TabId = (typeof TABS)[number]

export default function ScreenDetailPage() {
  const { id = '' } = useParams()
  const [sp, setSp] = useSearchParams()
  const tab: TabId = (TABS as readonly string[]).includes(sp.get('aba') ?? '') ? (sp.get('aba') as TabId) : 'status'
  const qc = useQueryClient()
  const can = useCan()
  const [dialog, setDialog] = React.useState<'status' | 'move' | 'decommission' | null>(null)
  const [newStatus, setNewStatus] = React.useState<string | null>(null)
  const [newEst, setNewEst] = React.useState<string | null>(null)

  const q = useQuery({ queryKey: qk.detail('screens', id), queryFn: () => screensApi.get(id) })
  const presence = useScreenPresence(q.data && q.data.status !== 'DECOMMISSIONED' ? id : undefined)
  const s = q.data

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['screens'] })
    void qc.invalidateQueries({ queryKey: ['establishments'] })
    void qc.invalidateQueries({ queryKey: ['inventory'] })
  }
  const setStatus = useMutation({
    mutationFn: (reason: string) => screensApi.setStatus(id, { status: newStatus!, reason }),
    onSuccess: (r) => {
      invalidate()
      toast.success(`Status alterado para “${labelOf(SCREEN_STATUS_LABEL, r.status)}”.`)
    },
  })
  const move = useMutation({
    mutationFn: (reason: string) => screensApi.move(id, { establishment_id: newEst!, reason }),
    onSuccess: (r) => {
      invalidate()
      toast.success(`Tela movida para ${r.establishment?.name ?? 'o novo estabelecimento'}. Código e histórico preservados.`)
    },
  })
  const decommission = useMutation({
    mutationFn: (reason: string) => screensApi.decommission(id, reason),
    onSuccess: () => {
      invalidate()
      toast.success('Tela retirada definitivamente.')
    },
  })

  const crumbs = React.useMemo(() => [{ label: 'Telas', to: '/telas' }, { label: s?.friendly_id ?? 'Tela' }], [s?.friendly_id])

  if (q.isLoading) return <PageSkeleton />
  if (isApiError(q.error) && q.error.isForbidden) return <NoPermission permission="screens.view" />
  if (q.error || !s)
    return (
      <ErrorState
        error={q.error}
        title={isApiError(q.error) && q.error.isNotFound ? 'Tela não encontrada ou fora do seu acesso' : undefined}
        onRetry={() => void q.refetch()}
        retrying={q.isFetching}
      />
    )

  const tz = s.establishment?.timezone
  const decommissioned = s.status === 'DECOMMISSIONED'
  const paired = !!presence.data?.paired
  const outReason = screenInventoryReason(s)

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title={`${s.friendly_id} · ${s.name}`}
        description={s.establishment ? `${s.establishment.name} — ${s.establishment.city}/${s.establishment.state}` : undefined}
        breadcrumbs={crumbs}
        actions={
          decommissioned ? undefined : (
            <div className="flex flex-wrap gap-2">
              <Can permission="screens.pair">
                <Button asChild variant="secondary">
                  <Link to={`/telas/parear?screen_id=${s.id}`}>
                    <QrCode aria-hidden="true" />
                    Parear
                  </Link>
                </Button>
              </Can>
              <Can permission="screens.manage">
                <Button variant="secondary" onClick={() => { setNewStatus(null); setDialog('status') }}>
                  <ToggleLeft aria-hidden="true" />
                  Status
                </Button>
                <Button variant="secondary" onClick={() => { setNewEst(null); setDialog('move') }}>
                  <ArrowRightLeft aria-hidden="true" />
                  Mover
                </Button>
                <Button variant="destructive" onClick={() => setDialog('decommission')}>
                  <Ban aria-hidden="true" />
                  Retirar
                </Button>
                <Button asChild>
                  <Link to={`/telas/${s.id}/editar`}>
                    <Pencil aria-hidden="true" />
                    Editar
                  </Link>
                </Button>
              </Can>
            </div>
          )
        }
      />
      <RealtimeStale updatedAt={presence.dataUpdatedAt || q.dataUpdatedAt} onRefresh={() => { void q.refetch(); void presence.refetch() }} refreshing={q.isFetching || presence.isFetching} />

      <div className="flex flex-wrap items-center gap-3">
        <Badge tone={screenStatusTone(s.status)}>{labelOf(SCREEN_STATUS_LABEL, s.status)}</Badge>
        <Badge tone={s.sellable ? 'success' : 'neutral'}>{s.sellable ? 'Vendável' : 'Fora do inventário'}</Badge>
        {!decommissioned ? (
          <PresenceBadge presence={presence.data ? presenceToSummary(presence.data) : undefined} loading={presence.isLoading} timeZone={tz} />
        ) : null}
      </div>

      {decommissioned ? (
        <Alert variant="warning">
          <AlertTitle>Tela retirada definitivamente</AlertTitle>
          <AlertDescription>
            Em {formatDateTime(s.decommissioned_at, tz)}. Motivo: {s.decommission_reason ?? '—'}. O histórico é mantido; a tela não volta ao inventário.
          </AlertDescription>
        </Alert>
      ) : !s.sellable ? (
        <Alert variant="info">
          <AlertDescription className="text-text">
            {outReason
              ? `Fora do inventário: ${lowerFirst(outReason.reason)}. ${outReason.next}`
              : 'Fora do inventário: a tela não entra na disponibilidade das campanhas. Confira o status da tela e do estabelecimento.'}
          </AlertDescription>
        </Alert>
      ) : null}

      <Tabs value={tab} onValueChange={(v) => setSp((p) => { const n = new URLSearchParams(p); n.set('aba', v); return n }, { replace: true })}>
        <TabsList aria-label="Seções da tela">
          <TabsTrigger value="status">Status e diagnóstico</TabsTrigger>
          <TabsTrigger value="comandos">Comandos</TabsTrigger>
          <TabsTrigger value="programacao">Programação</TabsTrigger>
          <TabsTrigger value="hardware">Hardware</TabsTrigger>
          <TabsTrigger value="audiencia">Audiência estimada</TabsTrigger>
          <TabsTrigger value="historico">Histórico</TabsTrigger>
        </TabsList>
        <TabsContent value="status">
          {decommissioned ? <p className="m-0 text-sm text-text-muted">Sem presença: tela retirada.</p> : <ScreenStatusTab screen={s} />}
        </TabsContent>
        <TabsContent value="comandos">
          <ScreenCommandsTab screen={s} paired={paired} />
        </TabsContent>
        <TabsContent value="programacao">
          <ScreenScheduleTab screen={s} />
        </TabsContent>
        <TabsContent value="hardware">
          <ScreenHardwareTab screen={s} />
        </TabsContent>
        <TabsContent value="audiencia">
          <AudiencePanel
            current={s.estimated_daily_audience}
            domain="screens"
            subjectId={s.id}
            list={() => screensApi.audience(s.id)}
            add={(body) => screensApi.addAudience(s.id, body)}
            canEdit={can('screens.manage') && !decommissioned}
            timeZone={tz}
          />
        </TabsContent>
        <TabsContent value="historico">
          <ScreenHistoryTab screen={s} />
        </TabsContent>
      </Tabs>

      <ReasonDialog
        open={dialog === 'status'}
        onOpenChange={(o) => setDialog(o ? 'status' : null)}
        title={`Alterar o status de ${s.friendly_id}`}
        description="Manutenção NÃO tira a tela do inventário; Inativa sai do inventário até ser reativada."
        confirmLabel="Alterar status"
        extraValid={!!newStatus && newStatus !== s.status}
        onConfirm={(reason) => setStatus.mutateAsync(reason)}
      >
        <Field required>
          <FieldLabel>Novo status</FieldLabel>
          <OptionSelect
            value={newStatus}
            onChange={setNewStatus}
            options={(['ACTIVE', 'MAINTENANCE', 'INACTIVE'] as const)
              .filter((x) => x !== s.status)
              .map((x) => ({ value: x, label: SCREEN_STATUS_LABEL[x] }))}
          />
        </Field>
      </ReasonDialog>
      <ReasonDialog
        open={dialog === 'move'}
        onOpenChange={(o) => setDialog(o ? 'move' : null)}
        title={`Mover ${s.friendly_id} para outro estabelecimento`}
        description="O código TEL-UF-XXXX, o histórico e as exibições já registradas são preservados."
        consequences={['A programação é regenerada no fuso do novo estabelecimento.', 'O dispositivo pareado continua vinculado à tela.']}
        confirmLabel="Mover tela"
        extraValid={!!newEst && newEst !== s.establishment_id}
        onConfirm={(reason) => move.mutateAsync(reason)}
      >
        <EstablishmentPicker value={newEst} onChange={setNewEst} required onlyActive legend="Novo estabelecimento" />
      </ReasonDialog>
      <ReasonDialog
        open={dialog === 'decommission'}
        onOpenChange={(o) => setDialog(o ? 'decommission' : null)}
        title={`Retirar definitivamente a tela ${s.friendly_id}?`}
        description="Ação definitiva e auditada. Para pausas temporárias use o status Manutenção ou Inativa."
        consequences={[
          'A tela sai do inventário de forma permanente e não pode ser reativada.',
          'Campanhas contratadas nesta tela entram no relatório de redistribuição.',
          'O histórico e as exibições registradas são mantidos.',
        ]}
        confirmLabel="Retirar definitivamente"
        destructive
        requireText={s.friendly_id}
        onConfirm={(reason) => decommission.mutateAsync(reason)}
      />
    </div>
  )
}
