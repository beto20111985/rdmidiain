/**
 * Fila de análise/aprovação (PG-09; RD, `campaigns.approve`): campanhas EM_ANALISE ordenadas
 * pelo prazo (SLA), compra ou alteração (a versão anterior continua no ar), anúncios com
 * código RD, aprovar/rejeitar com motivo, aprovação/rejeição em massa com resultado por item
 * e política de aprovação por cliente. Atualiza por `approval.pending`/`campaign.updated`.
 */
import * as React from 'react'
import { Link } from 'react-router-dom'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { AlarmClock, CheckCheck, CheckCircle2, XCircle } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Switch } from '@/components/ui/switch'
import { Field, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { ConfirmDialog } from '@/components/patterns/confirm-dialog'
import { ErrorState, StaleData } from '@/components/patterns/states'
import { ReasonDialog } from '@/components/campaigns/reason-dialog'
import { ToneBadge } from '@/components/campaigns/badges'
import { OrgName } from '@/components/campaigns/org-name'
import { EntityPicker, type PickerOption } from '@/components/campaigns/entity-picker'
import { useTableState } from '@/hooks/useTableState'
import { useRealtimeStatus } from '@/lib/realtime/context'
import { qk } from '@/lib/queryKeys'
import { newRequestId } from '@/lib/http'
import { formatCents, formatDateTime, formatNumber } from '@/lib/format'
import { campaignsApi } from './lib/api'
import { periodText } from './lib/format'
import { searchOrganizations } from './lib/pickers'
import type { ReviewQueueItem } from './lib/types'
import { CreativeReviewTab } from './approvals/CreativeReviewTab'
import { codeText, describeError, withDescribedError } from '@/pages/campaigns/lib/errorText'

type BulkResult = { action: string; done: string[]; failed: Array<{ campaign_id: string; code: string; message: string }> }

export default function ApprovalQueuePage() {
  return (
    <>
      <PageHeader title="Aprovação de campanhas" description="Pagamento não aprova: toda campanha passa por esta análise, salvo política automática." />
      <Tabs defaultValue="queue">
        <TabsList>
          <TabsTrigger value="queue">Fila de análise</TabsTrigger>
          <TabsTrigger value="creatives">Conteúdo interno e institucional</TabsTrigger>
          <TabsTrigger value="policies">Política de aprovação</TabsTrigger>
        </TabsList>
        <TabsContent value="queue">
          <Queue />
        </TabsContent>
        <TabsContent value="creatives">
          <CreativeReviewTab />
        </TabsContent>
        <TabsContent value="policies">
          <Policies />
        </TabsContent>
      </Tabs>
    </>
  )
}

function Queue() {
  const qc = useQueryClient()
  const { live } = useRealtimeStatus()
  const table = useTableState({ defaultPageSize: 25, filterKeys: ['overdue_only'] })
  const [selected, setSelected] = React.useState<string[]>([])
  const [dialog, setDialog] = React.useState<{ kind: 'approve' | 'reject' | 'bulk-approve' | 'bulk-reject'; item?: ReviewQueueItem } | null>(null)
  const [bulkResult, setBulkResult] = React.useState<BulkResult | null>(null)
  const params = { page: table.apiParams.page, page_size: table.apiParams.page_size, overdue_only: table.state.filters.overdue_only ? true : undefined }
  const q = useQuery({
    queryKey: qk.list('approvals', params),
    queryFn: () => campaignsApi.reviewQueue(params),
    placeholderData: keepPreviousData,
  })
  const items = q.data?.items ?? []
  const refresh = () => {
    setSelected([])
    void qc.invalidateQueries({ queryKey: qk.root('approvals') })
    void qc.invalidateQueries({ queryKey: qk.root('campaigns') })
  }
  const toggle = (id: string, on: boolean) => setSelected((s) => (on ? [...new Set([...s, id])] : s.filter((x) => x !== id)))
  const allOnPage = items.length > 0 && items.every((i) => selected.includes(i.id))

  const cols: DataTableColumn<ReviewQueueItem>[] = [
    {
      id: 'sel',
      header: 'Selecionar',
      mobile: 'field',
      cell: (r) => <Checkbox checked={selected.includes(r.id)} onCheckedChange={(c) => toggle(r.id, c === true)} aria-label={`Selecionar ${r.name}`} />,
    },
    {
      id: 'name',
      header: 'Campanha',
      mobile: 'primary',
      cell: (r) => (
        <span className="flex flex-col">
          <Link className="font-semibold text-link underline-offset-4 [overflow-wrap:anywhere] hover:underline" to={`/campanhas/${r.id}`}>
            {r.name}
          </Link>
          <span className="text-xs text-text-muted">
            <OrgName id={r.org_id} />
          </span>
        </span>
      ),
    },
    {
      id: 'kind',
      header: 'Tipo',
      mobile: 'badge',
      cell: (r) => (r.is_change_request ? <ToneBadge label="Alteração (versão anterior no ar)" tone="info" /> : <ToneBadge label="Nova compra" tone="neutral" />),
    },
    {
      id: 'sla',
      header: 'Prazo (SLA)',
      cell: (r) =>
        r.sla_overdue ? (
          <ToneBadge label={`Atrasada · ${formatDateTime(r.review_due_at)}`} tone="danger" icon={AlarmClock} />
        ) : (
          <span>{formatDateTime(r.review_due_at)}</span>
        ),
    },
    {
      id: 'creatives',
      header: 'Anúncios',
      cell: (r) => r.creatives.filter((c) => !c.pending_removal).map((c) => c.code).join(', ') || '—',
    },
    { id: 'period', header: 'Vigência', cell: (r) => periodText(r) },
    {
      id: 'price',
      header: 'Valor',
      numeric: true,
      cell: (r) => formatCents(r.pending_revision?.price_cents ?? r.contracted_price_cents),
    },
  ]

  return (
    <div className="flex flex-col gap-6">
      {!live && q.data ? <StaleData updatedAt={q.dataUpdatedAt} reason="tempo real indisponível; novas entradas na fila podem não aparecer." onRefresh={() => void q.refetch()} refreshing={q.isFetching} /> : null}
      {bulkResult ? (
        <Alert variant={bulkResult.failed.length ? 'warning' : 'success'}>
          <div>
            <AlertTitle>
              {bulkResult.action === 'approve' ? 'Aprovação' : 'Rejeição'} em massa: {bulkResult.done.length} concluída(s), {bulkResult.failed.length} com falha
            </AlertTitle>
            {bulkResult.failed.length ? (
              <AlertDescription>
                <ul className="m-0 list-disc pl-5">
                  {bulkResult.failed.map((f) => (
                    <li key={f.campaign_id}>
                      {items.find((i) => i.id === f.campaign_id)?.name ?? f.campaign_id}: {codeText(f.code, f.message)}
                    </li>
                  ))}
                </ul>
              </AlertDescription>
            ) : null}
          </div>
        </Alert>
      ) : null}
      <Card>
        <DataTable
          caption="Campanhas em análise"
          columns={cols}
          data={q.data}
          getRowId={(r) => r.id}
          controller={table}
          isLoading={q.isLoading}
          isFetching={q.isFetching}
          error={q.error}
          onRetry={() => void q.refetch()}
          filtersActive={!!table.state.filters.overdue_only}
          empty={{ title: 'Nenhuma campanha aguardando análise', description: 'Novas compras e alterações aparecem aqui em tempo real.', icon: CheckCheck }}
          rowActions={(r) => (
            <>
              <Button variant="secondary" onClick={() => setDialog({ kind: 'approve', item: r })}>
                <CheckCircle2 aria-hidden="true" />
                Aprovar
              </Button>
              <Button variant="ghost" onClick={() => setDialog({ kind: 'reject', item: r })}>
                <XCircle aria-hidden="true" />
                Rejeitar
              </Button>
            </>
          )}
          toolbar={
            <>
              <Field orientation="horizontal" className="min-h-11 w-auto">
                <Switch checked={!!table.state.filters.overdue_only} onCheckedChange={(v) => table.setFilter('overdue_only', v ? '1' : null)} />
                <FieldLabel className="font-normal">Somente atrasadas (SLA vencido)</FieldLabel>
              </Field>
              <Field orientation="horizontal" className="min-h-11 w-auto">
                <Checkbox checked={allOnPage} onCheckedChange={(c) => items.forEach((i) => toggle(i.id, c === true))} disabled={!items.length} />
                <FieldLabel className="font-normal">Selecionar todas da página</FieldLabel>
              </Field>
              <div className="flex flex-wrap gap-2">
                <Button onClick={() => setDialog({ kind: 'bulk-approve' })} disabled={!selected.length}>
                  Aprovar selecionadas ({selected.length})
                </Button>
                <Button variant="destructive" onClick={() => setDialog({ kind: 'bulk-reject' })} disabled={!selected.length}>
                  Rejeitar selecionadas
                </Button>
              </div>
            </>
          }
        />
      </Card>

      <ConfirmDialog
        open={dialog?.kind === 'approve'}
        onOpenChange={(o) => !o && setDialog(null)}
        title={`Aprovar ${dialog?.item?.name ?? ''}?`}
        description={dialog?.item?.is_change_request ? 'A nova versão substitui a atual nas telas.' : 'A campanha será publicada para as telas contratadas.'}
        confirmLabel="Aprovar"
        onConfirm={async () => {
          await withDescribedError(() => campaignsApi.approve(dialog!.item!.id, null, newRequestId()))
          toast.success('Campanha aprovada.')
          refresh()
        }}
      />
      <ReasonDialog
        open={dialog?.kind === 'reject'}
        onOpenChange={(o) => !o && setDialog(null)}
        destructive
        title={`Rejeitar ${dialog?.item?.name ?? ''}?`}
        description="O anunciante recebe o motivo e pode corrigir e reenviar."
        reasonLabel="Motivo da rejeição (visível ao anunciante)"
        minLength={10}
        confirmLabel="Rejeitar"
        onConfirm={async (reason) => {
          await campaignsApi.reject(dialog!.item!.id, reason, null, newRequestId())
          toast.success('Campanha rejeitada.')
          refresh()
        }}
      />
      <BulkConfirm
        open={dialog?.kind === 'bulk-approve'}
        onOpenChange={(o) => !o && setDialog(null)}
        count={selected.length}
        onConfirm={async () => {
          const r = await campaignsApi.bulk(selected, 'approve', null, newRequestId())
          setBulkResult(r)
          refresh()
        }}
      />
      <ReasonDialog
        open={dialog?.kind === 'bulk-reject'}
        onOpenChange={(o) => !o && setDialog(null)}
        destructive
        title={`Rejeitar ${selected.length} campanha(s)?`}
        description="O mesmo motivo é enviado a todos os anunciantes selecionados."
        minLength={10}
        confirmLabel="Rejeitar selecionadas"
        onConfirm={async (reason) => {
          const r = await campaignsApi.bulk(selected, 'reject', reason, newRequestId())
          setBulkResult(r)
          refresh()
        }}
      />
      <p className="m-0 text-xs text-text-muted" aria-live="polite">
        {q.data ? `${formatNumber(q.data.total)} campanha(s) na fila.` : ''}
      </p>
    </div>
  )
}

function BulkConfirm({ open, onOpenChange, count, onConfirm }: { open: boolean; onOpenChange: (o: boolean) => void; count: number; onConfirm: () => Promise<void> }) {
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Aprovar ${count} campanha(s)?`}
      description="Cada campanha é aprovada individualmente; falhas não impedem as demais e aparecem no resultado."
      confirmLabel="Aprovar selecionadas"
      onConfirm={onConfirm}
    />
  )
}

function Policies() {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: qk.sub('approvals', 'policies'), queryFn: campaignsApi.approvalPolicies })
  const [org, setOrg] = React.useState<PickerOption | null>(null)
  const [mode, setMode] = React.useState<'MANUAL' | 'AUTO'>('AUTO')
  const [open, setOpen] = React.useState(false)
  const [removing, setRemoving] = React.useState<string | null>(null)
  const remove = useMutation({
    mutationFn: (orgId: string) => campaignsApi.removeApprovalPolicy(orgId),
    onSuccess: () => {
      toast.success('Exceção removida.')
      void qc.invalidateQueries({ queryKey: qk.sub('approvals', 'policies') })
    },
  })
  if (q.isLoading) return <p className="text-sm text-text-muted">Carregando…</p>
  if (q.error) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />
  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardTitle>Regra global</CardTitle>
        <CardDescription>
          Modo atual: <strong>{q.data?.global_mode === 'AUTO' ? 'Aprovação automática' : 'Análise manual pela RD'}</strong> (configurável em Configurações do sistema).
        </CardDescription>
      </Card>
      <Card>
        <CardTitle>Exceções por cliente (cliente confiável)</CardTitle>
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-64 flex-1">
            <EntityPicker label="Cliente" domain="organizations" value={org} onChange={setOrg} search={(t) => searchOrganizations(t, 'ADVERTISER')} />
          </div>
          <Field className="w-56">
            <FieldLabel>Modo</FieldLabel>
            <Select value={mode} onValueChange={(v) => setMode(v as 'MANUAL' | 'AUTO')}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  <SelectItem value="AUTO">Aprovação automática</SelectItem>
                  <SelectItem value="MANUAL">Sempre análise manual</SelectItem>
                </SelectGroup>
              </SelectContent>
            </Select>
          </Field>
          <Button onClick={() => setOpen(true)} disabled={!org}>
            Definir exceção
          </Button>
        </div>
        {q.data?.items.length ? (
          <Table>
            <caption className="sr-only">Exceções de aprovação</caption>
            <TableHeader>
              <TableRow className="h-11 hover:bg-transparent">
                <TableHead scope="col">Cliente</TableHead>
                <TableHead scope="col">Modo</TableHead>
                <TableHead scope="col">Motivo</TableHead>
                <TableHead scope="col">Atualizado</TableHead>
                <TableHead scope="col" className="text-right">
                  Ações
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {q.data.items.map((p) => (
                <TableRow key={p.org_id}>
                  <TableCell>
                    <OrgName id={p.org_id} />
                  </TableCell>
                  <TableCell>{p.mode === 'AUTO' ? 'Automática' : 'Manual'}</TableCell>
                  <TableCell>{p.reason}</TableCell>
                  <TableCell>{formatDateTime(p.updated_at)}</TableCell>
                  <TableCell className="text-right">
                    <Button variant="ghost" onClick={() => setRemoving(p.org_id)}>
                      Remover
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : (
          <p className="m-0 text-sm text-text-muted">Nenhuma exceção: vale a regra global.</p>
        )}
      </Card>
      <ReasonDialog
        open={open}
        onOpenChange={setOpen}
        title={`Definir ${mode === 'AUTO' ? 'aprovação automática' : 'análise manual'} para ${org?.label ?? ''}?`}
        minLength={10}
        confirmLabel="Salvar exceção"
        onConfirm={async (reason) => {
          await campaignsApi.setApprovalPolicy(org!.id, mode, reason)
          toast.success('Exceção salva.')
          setOrg(null)
          void qc.invalidateQueries({ queryKey: qk.sub('approvals', 'policies') })
        }}
      />
      <ConfirmDialog
        open={!!removing}
        onOpenChange={(o) => !o && setRemoving(null)}
        title="Remover exceção?"
        description="O cliente volta a seguir a regra global."
        confirmLabel="Remover"
        destructive
        onConfirm={async () => {
          if (removing) await remove.mutateAsync(removing)
        }}
      />
      {remove.error ? <p className="text-sm text-danger">{describeError(remove.error)}</p> : null}
    </div>
  )
}
