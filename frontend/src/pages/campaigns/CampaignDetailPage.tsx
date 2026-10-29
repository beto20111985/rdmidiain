/**
 * Detalhe da campanha (PG-08/PG-09): status PT + REPRODUZINDO (derivado de exibição comprovada),
 * histórico de transições, anúncios com código RD, revisões/alterações pendentes (a versão
 * anterior continua tocando), telas contratadas (anunciante: código da tela e UF, sem nomes),
 * comprovantes, pausar/retomar, cancelar (com cotação de reembolso), duplicar, renovar,
 * alteração pós-aprovação, e — equipe RD — aprovar/rejeitar e kill emergencial.
 */
import * as React from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import {
  CheckCircle2,
  Copy,
  FileCheck2,
  OctagonX,
  Pause,
  Pencil,
  Play,
  RefreshCcw,
  Send,
  ShoppingCart,
  XCircle,
} from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Switch } from '@/components/ui/switch'
import { Field, FieldLabel } from '@/components/ui/field'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Skeleton } from '@/components/ui/skeleton'
import { ConfirmDialog } from '@/components/patterns/confirm-dialog'
import { ErrorState, NoPermission, PageSkeleton, StaleData } from '@/components/patterns/states'
import { CampaignStatusBadge, PlayingBadge, ToneBadge } from '@/components/campaigns/badges'
import { ReasonDialog } from '@/components/campaigns/reason-dialog'
import { RulePath } from '@/components/campaigns/rule-path'
import { EstablishmentName, OrgName } from '@/components/campaigns/org-name'
import { QuoteBreakdown } from '@/components/campaigns/quote-breakdown'
import { useCan } from '@/hooks/usePermissions'
import { useRealtimeStatus } from '@/lib/realtime/context'
import { qk } from '@/lib/queryKeys'
import { newRequestId } from '@/lib/http'
import { isApiError } from '@/lib/errors'
import { EMPTY_VALUE, formatCents, formatDateTime, formatNumber } from '@/lib/format'
import { campaignsApi } from './lib/api'
import { CAMPAIGN_STATUS, CREATIVE_STATUS, EXCLUSIVITY_LABEL, PRIORITY_LABEL, TECHNICAL_STATUS, TRIGGER_LABEL, campaignStatusLabel } from './lib/labels'
import { bandsText, daysText, frequencyText, periodText, playingText, targetText } from './lib/format'
import { cityKeyLabel } from './lib/rules'
import { useCampaignArea } from './lib/hooks'
import type { CampaignDetail, Revision } from './lib/types'
import { NewCreative } from './wizard/StepCreatives'
import { ChangeRequestDialog } from './detail/ChangeRequestDialog'
import { CancelCampaignDialog, RenewalCard, RenewDialog } from './detail/ActionDialogs'
import { describeError, withDescribedError } from '@/pages/campaigns/lib/errorText'

type DialogKind = 'pause' | 'cancel' | 'duplicate' | 'renew' | 'kill' | 'approve' | 'reject' | 'change' | null
const OPERATIONAL = ['APROVADA', 'AGENDADA', 'SINCRONIZANDO', 'ATIVA', 'PAUSADA']
const RECURRING = ['MENSAL', 'SEMESTRAL', 'ANUAL']
const CREATIVE_EDITABLE = ['RASCUNHO', 'AGUARDANDO_MIDIA', 'REJEITADA']

export default function CampaignDetailPage() {
  const { id = '' } = useParams()
  const area = useCampaignArea()
  const can = useCan()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const { live } = useRealtimeStatus()
  const [dialog, setDialog] = React.useState<DialogKind>(null)
  const [addingCreative, setAddingCreative] = React.useState(false)

  const q = useQuery({ queryKey: qk.detail('campaigns', id), queryFn: () => campaignsApi.get(id), enabled: !!id })
  const history = useQuery({ queryKey: qk.sub('campaigns', 'detail', id, 'history'), queryFn: () => campaignsApi.history(id), enabled: !!q.data })
  const revisions = useQuery({ queryKey: qk.sub('campaigns', 'detail', id, 'revisions'), queryFn: () => campaignsApi.revisions(id), enabled: !!q.data })
  const screens = useQuery({
    queryKey: qk.sub('campaigns', 'detail', id, 'screens'),
    queryFn: () => campaignsApi.screens(id),
    enabled: !!q.data && (q.data.has_live_version || q.data.targets.length > 0),
    retry: false,
  })
  const conflicts = useQuery({
    queryKey: qk.sub('campaigns', 'detail', id, 'conflicts'),
    queryFn: () => campaignsApi.conflicts(id),
    enabled: !!q.data && q.data.targets.length > 0,
    retry: false,
  })

  const setCampaign = (c: CampaignDetail) => {
    qc.setQueryData(qk.detail('campaigns', id), c)
    void qc.invalidateQueries({ queryKey: qk.root('campaigns') })
  }
  const act = useMutation({
    mutationFn: async (fn: () => Promise<CampaignDetail>) => fn(),
    onSuccess: (c) => setCampaign(c),
    onError: (e) => toast.error(describeError(e)),
  })

  const breadcrumbs = React.useMemo(
    () => [{ label: 'Campanhas', to: area.campaignsBase }, { label: q.data?.name ?? 'Campanha' }],
    [area.campaignsBase, q.data?.name],
  )

  if (q.isLoading) return <PageSkeleton label="Carregando campanha…" />
  if (q.error || !q.data) {
    if (isApiError(q.error) && q.error.isForbidden) return <NoPermission permission="campaigns.view" />
    return (
      <ErrorState
        error={q.error}
        title={isApiError(q.error) && q.error.isNotFound ? 'Campanha não encontrada ou fora do seu acesso' : undefined}
        onRetry={() => void q.refetch()}
      />
    )
  }
  const c = q.data
  const t = c.allowed_transitions ?? {}
  const manage = can('campaigns.manage')
  const operational = OPERATIONAL.includes(c.status)
  const renewBlocked = !RECURRING.includes(c.period_type)
    ? 'Somente campanhas mensais, semestrais ou anuais são renováveis.'
    : !operational
      ? 'Disponível apenas para campanhas aprovadas e vigentes.'
      : c.has_pending_change
        ? 'Já existe uma alteração/renovação pendente.'
        : null
  const statusInfo = (CAMPAIGN_STATUS as Record<string, { hint: string }>)[c.status]

  const actions = (
    <div className="flex flex-wrap gap-2">
      {c.status === 'RASCUNHO' && manage ? (
        <Button variant="secondary" asChild>
          <Link to={`${area.newCampaignPath}?id=${c.id}&etapa=1`}>
            <Pencil aria-hidden="true" />
            Continuar edição
          </Link>
        </Button>
      ) : null}
      {c.status === 'RASCUNHO' && !area.isStaff && can('checkout.create') ? (
        <Button asChild>
          <Link to={`${area.cartPath}?c=${c.id}`}>
            <ShoppingCart aria-hidden="true" />
            Ir para o pagamento
          </Link>
        </Button>
      ) : null}
      {area.isStaff && c.status === 'EM_ANALISE' && can('campaigns.approve') ? (
        <>
          <Button onClick={() => setDialog('approve')}>
            <CheckCircle2 aria-hidden="true" />
            Aprovar
          </Button>
          <Button variant="destructive" onClick={() => setDialog('reject')}>
            <XCircle aria-hidden="true" />
            Rejeitar
          </Button>
        </>
      ) : null}
    </div>
  )

  return (
    <>
      <PageHeader title={c.name} breadcrumbs={breadcrumbs} description={area.isStaff ? <OrgName id={c.org_id} /> : undefined} actions={actions} />
      <div className="flex flex-col gap-6">
        {!live ? <StaleData updatedAt={q.dataUpdatedAt} reason="tempo real indisponível; status pode estar desatualizado." onRefresh={() => void q.refetch()} refreshing={q.isFetching} /> : null}

        <Card>
          <CardHeader>
            <div className="flex flex-wrap items-center gap-2">
              <CampaignStatusBadge status={c.status} />
              <PlayingBadge campaign={c} />
            </div>
          </CardHeader>
          <p className="m-0 text-sm text-text-muted">{statusInfo?.hint}</p>
          <p className="m-0 text-sm">{playingText(c.reproducing)}</p>
          {c.status === 'REJEITADA' && c.rejection_reason ? (
            <Alert variant="danger">
              <AlertTitle>Motivo da rejeição</AlertTitle>
              <AlertDescription>{c.rejection_reason}</AlertDescription>
            </Alert>
          ) : null}
          {c.payment_issue ? (
            <Alert variant="warning">
              <AlertDescription>Pendência de pagamento: {c.payment_issue}</AlertDescription>
            </Alert>
          ) : null}
          {c.has_pending_change && c.pending_revision?.kind !== 'RENEWAL' ? (
            <Alert variant="info">
              <AlertTitle>Alteração em análise</AlertTitle>
              <AlertDescription>A versão aprovada anterior continua no ar até a decisão da RD.</AlertDescription>
            </Alert>
          ) : null}
          {c.status === 'EM_ANALISE' && c.review_due_at ? (
            <p className="m-0 text-sm">Prazo de análise (SLA): {formatDateTime(c.review_due_at)}</p>
          ) : null}

          <div className="flex flex-wrap gap-2 border-t border-border pt-4">
            {t.pause && manage ? (
              <Button variant="secondary" onClick={() => setDialog('pause')}>
                <Pause aria-hidden="true" />
                Pausar
              </Button>
            ) : null}
            {t.resume && manage ? (
              <Button variant="secondary" loading={act.isPending} onClick={() => act.mutate(() => campaignsApi.resume(c.id))}>
                <Play aria-hidden="true" />
                Retomar
              </Button>
            ) : null}
            {c.status === 'AGUARDANDO_MIDIA' && manage ? (
              <Button loading={act.isPending} onClick={() => act.mutate(() => campaignsApi.submitMedia(c.id))} disabled={!c.creatives.some((x) => x.is_active || x.pending)}>
                <Send aria-hidden="true" />
                Enviar anúncios para análise
              </Button>
            ) : null}
            {c.status === 'REJEITADA' && manage && c.paid_at ? (
              <Button loading={act.isPending} onClick={() => act.mutate(() => campaignsApi.resubmit(c.id))}>
                <Send aria-hidden="true" />
                Reenviar para análise
              </Button>
            ) : null}
            {operational && manage && !c.has_pending_change ? (
              <Button variant="secondary" onClick={() => setDialog('change')}>
                <Pencil aria-hidden="true" />
                Solicitar alteração
              </Button>
            ) : null}
            {can('checkout.create') && !area.isStaff ? (
              <Button variant="secondary" onClick={() => setDialog('renew')} disabled={!!renewBlocked} title={renewBlocked ?? undefined}>
                <RefreshCcw aria-hidden="true" />
                Renovar
              </Button>
            ) : null}
            {manage ? (
              <Button variant="secondary" onClick={() => setDialog('duplicate')}>
                <Copy aria-hidden="true" />
                Duplicar
              </Button>
            ) : null}
            <Button variant="secondary" asChild>
              <Link to={area.proofsPath}>
                <FileCheck2 aria-hidden="true" />
                Comprovantes
              </Link>
            </Button>
            {t.cancel && manage ? (
              <Button variant="destructive" onClick={() => setDialog('cancel')}>
                <XCircle aria-hidden="true" />
                Cancelar campanha
              </Button>
            ) : null}
            {area.isStaff && t.kill && can('campaigns.kill') ? (
              <Button variant="destructive" onClick={() => setDialog('kill')}>
                <OctagonX aria-hidden="true" />
                Kill emergencial
              </Button>
            ) : null}
          </div>
          {renewBlocked && can('checkout.create') && !area.isStaff ? <p className="m-0 text-xs text-text-muted">Renovar: {renewBlocked}</p> : null}
          {RECURRING.includes(c.period_type) && operational ? (
            <RenewalCard
              campaignId={c.id}
              canPay={can('checkout.create') && !area.isStaff}
              checkoutPath={area.checkoutPath}
              onCheckout={(orderId) => {
                toast.success('Cobrança da renovação gerada. As telas ficam reservadas por 2 horas.')
                navigate(area.checkoutPath(orderId))
              }}
            />
          ) : null}
          {RECURRING.includes(c.period_type) && manage && (operational || c.status === 'RASCUNHO') ? (
            <Field orientation="horizontal" className="min-h-11">
              <Switch
                checked={!!c.auto_renew}
                onCheckedChange={(v) =>
                  void campaignsApi
                    .setAutoRenew(c.id, v)
                    .then(() => q.refetch())
                    .then(() => toast.success(v ? 'Renovação automática ligada.' : 'Renovação automática desligada.'))
                    .catch((e) => toast.error(describeError(e)))
                }
              />
              <FieldLabel className="font-normal">Renovação automática (PIX gerado antes do vencimento)</FieldLabel>
            </Field>
          ) : null}
        </Card>

        <div className="grid grid-cols-1 gap-6 xl:grid-cols-12">
          <Card className="xl:col-span-6">
            <CardTitle>Resumo</CardTitle>
            <dl className="m-0 grid grid-cols-1 gap-3 text-sm sm:grid-cols-2">
              {(
                [
                  ['Período', periodText(c)],
                  ['Duração do anúncio', `${c.duration_s} s`],
                  ['Dias', daysText(c.days_mask)],
                  ['Faixas horárias', bandsText(c.time_bands)],
                  ['Frequência', frequencyText(c)],
                  ['Intervalo mínimo', c.min_interval_s ? `${Math.round(c.min_interval_s / 60)} min` : 'Sem intervalo mínimo'],
                  ['Prioridade', PRIORITY_LABEL[c.priority]],
                  ['Exclusividade', EXCLUSIVITY_LABEL[c.exclusivity]],
                  ['Valor contratado', c.contracted_price_cents > 0 ? formatCents(c.contracted_price_cents) : EMPTY_VALUE],
                  ['Pago em', formatDateTime(c.paid_at)],
                  ['Aprovada em', formatDateTime(c.approved_at)],
                  ['Criada em', formatDateTime(c.created_at)],
                ] as Array<[string, string]>
              ).map(([k, v]) => (
                <div key={k} className="flex flex-col">
                  <dt className="text-xs text-text-muted">{k}</dt>
                  <dd className="m-0">{v}</dd>
                </div>
              ))}
            </dl>
            <div className="flex flex-col gap-1">
              <span className="text-xs text-text-muted">Segmentação</span>
              {c.targets.length ? (
                <ul className="m-0 flex list-disc flex-col gap-1 pl-5 text-sm">
                  {c.targets.map((x) => (
                    <li key={`${x.type}-${x.value}-${x.exclude}`}>{targetText(x)}</li>
                  ))}
                </ul>
              ) : (
                <span className="text-sm">{EMPTY_VALUE}</span>
              )}
            </div>
          </Card>

          <Card className="xl:col-span-6">
            <CardHeader>
              <CardTitle>Anúncios</CardTitle>
              {CREATIVE_EDITABLE.includes(c.status) && manage && !addingCreative ? (
                <Button variant="secondary" onClick={() => setAddingCreative(true)}>
                  Adicionar anúncio
                </Button>
              ) : null}
            </CardHeader>
            {c.creatives.length === 0 ? (
              <p className="m-0 text-sm text-text-muted">Nenhum anúncio vinculado.</p>
            ) : (
              <Table>
                <caption className="sr-only">Anúncios da campanha</caption>
                <TableHeader>
                  <TableRow className="h-11 hover:bg-transparent">
                    <TableHead scope="col">Código RD</TableHead>
                    <TableHead scope="col">Nome</TableHead>
                    <TableHead scope="col">Situação</TableHead>
                    {CREATIVE_EDITABLE.includes(c.status) && manage ? (
                      <TableHead scope="col" className="text-right">
                        Ações
                      </TableHead>
                    ) : null}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {c.creatives.map((l) => (
                    <TableRow key={l.creative_id}>
                      <TableCell className="font-mono font-semibold">{l.code ?? EMPTY_VALUE}</TableCell>
                      <TableCell>{l.name ?? EMPTY_VALUE}</TableCell>
                      <TableCell>
                        <span className="flex flex-wrap gap-1">
                          {l.status ? <ToneBadge {...CREATIVE_STATUS[l.status]} /> : null}
                          {l.technical_status ? <ToneBadge {...(TECHNICAL_STATUS[l.technical_status] ?? { label: l.technical_status, tone: 'neutral' })} /> : null}
                          {l.pending ? <ToneBadge label="Nova versão em análise" tone="warning" /> : null}
                          {l.pending_removal ? <ToneBadge label="Remoção em análise" tone="warning" /> : null}
                          {!l.is_active && !l.pending ? <ToneBadge label="Fora do ar" tone="neutral" /> : null}
                        </span>
                      </TableCell>
                      {CREATIVE_EDITABLE.includes(c.status) && manage ? (
                        <TableCell className="text-right">
                          <Button
                            variant="ghost"
                            onClick={() =>
                              void campaignsApi
                                .unlinkCreative(c.id, l.creative_id)
                                .then(() => q.refetch())
                                .catch((e) => toast.error(describeError(e)))
                            }
                            aria-label={`Remover ${l.code ?? 'anúncio'}`}
                          >
                            Remover
                          </Button>
                        </TableCell>
                      ) : null}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
            {addingCreative ? (
              <NewCreative
                orgId={c.org_id}
                campaignId={c.id}
                duration={c.duration_s}
                onCancel={() => setAddingCreative(false)}
                onDone={async () => {
                  setAddingCreative(false)
                  await q.refetch()
                }}
              />
            ) : null}
          </Card>
        </div>

        <ContractedScreensCard campaign={c} isStaff={area.isStaff} screens={screens} />

        <div className="grid grid-cols-1 gap-6 xl:grid-cols-12">
          <Card className="xl:col-span-7">
            <CardTitle>Versões e alterações</CardTitle>
            <RevisionsTable revisions={revisions.data?.items} loading={revisions.isLoading} error={revisions.error} liveId={c.live_revision?.id} pendingId={c.pending_revision?.id} />
            {c.pending_revision?.quote ? (
              <details className="rounded-lg border border-border p-3">
                <summary className="min-h-11 cursor-pointer content-center text-sm font-semibold">Preço da versão pendente (servidor)</summary>
                <QuoteBreakdown quote={c.pending_revision.quote} showGroups={false} />
              </details>
            ) : null}
          </Card>
          <Card className="xl:col-span-5">
            <CardTitle>Histórico de status</CardTitle>
            {history.isLoading ? (
              <Skeleton className="h-32 w-full" />
            ) : history.error ? (
              <ErrorState error={history.error} size="compact" onRetry={() => void history.refetch()} />
            ) : (
              <ol className="m-0 flex list-none flex-col gap-3 p-0">
                {(history.data?.items ?? []).slice().reverse().map((h, i) => (
                  <li key={i} className="flex flex-col border-l-2 border-border pl-3 text-sm">
                    <span className="font-semibold">
                      {h.from ? `${campaignStatusLabel(h.from)} → ` : ''}
                      {campaignStatusLabel(h.to)}
                    </span>
                    <span className="text-xs text-text-muted">
                      {formatDateTime(h.at)} · {TRIGGER_LABEL[h.trigger] ?? h.trigger}
                    </span>
                    {h.reason ? <span className="text-xs">{h.reason}</span> : null}
                  </li>
                ))}
              </ol>
            )}
          </Card>
        </div>

        {conflicts.data ? (
          <Card>
            <CardTitle>Conflitos</CardTitle>
            <CardDescription>
              {formatNumber(conflicts.data.allowed_screens)} tela(s) liberada(s) · {formatNumber(conflicts.data.blocked_establishments)} local(is) bloqueado(s) por
              conflito{area.isStaff ? '' : ' (nomes não são exibidos)'}.
            </CardDescription>
            {area.isStaff && conflicts.data.blocked?.length ? (
              <ul className="m-0 flex list-none flex-col gap-3 p-0">
                {conflicts.data.blocked.map((b) => (
                  <li key={b.establishment_id} className="flex flex-col gap-2 rounded-lg border border-border p-3">
                    <span className="font-semibold">
                      <EstablishmentName id={b.establishment_id} />
                    </span>
                    <RulePath decision={b} />
                  </li>
                ))}
              </ul>
            ) : null}
          </Card>
        ) : null}
      </div>

      <ReasonDialog
        open={dialog === 'pause'}
        onOpenChange={(o) => !o && setDialog(null)}
        title="Pausar campanha?"
        description="As telas deixam de exibir os anúncios desta campanha até a retomada. O período contratado não é estendido."
        optional
        minLength={0}
        confirmLabel="Pausar"
        onConfirm={async (reason) => setCampaign(await campaignsApi.pause(c.id, reason))}
      />
      <CancelCampaignDialog campaign={c} open={dialog === 'cancel'} onOpenChange={(o) => !o && setDialog(null)} onDone={(x) => { setCampaign(x); toast.success('Campanha cancelada.') }} />
      <RenewDialog
        campaign={c}
        open={dialog === 'renew'}
        onOpenChange={(o) => !o && setDialog(null)}
        onDone={() => {
          toast.success('Proposta de renovação calculada. Gere a cobrança para reservar as telas.')
          void qc.invalidateQueries({ queryKey: qk.sub('campaigns', 'detail', id, 'renewal') })
          void q.refetch()
        }}
      />
      <ConfirmDialog
        open={dialog === 'duplicate'}
        onOpenChange={(o) => !o && setDialog(null)}
        title={`Duplicar a campanha ${c.name}?`}
        description="Cria um novo rascunho com a mesma segmentação, programação e anúncios. O preço é recalculado no novo checkout."
        confirmLabel="Duplicar"
        onConfirm={async () => {
          const d = await campaignsApi.duplicate(c.id)
          void qc.invalidateQueries({ queryKey: qk.root('campaigns') })
          toast.success('Rascunho duplicado.')
          navigate(`${area.newCampaignPath}?id=${d.id}&etapa=1`)
        }}
      />
      <ReasonDialog
        open={dialog === 'kill'}
        onOpenChange={(o) => !o && setDialog(null)}
        destructive
        title="Kill emergencial"
        description="Interrompe IMEDIATAMENTE a exibição em todas as telas (comando de revogação). Use só em emergência: conteúdo indevido, ordem judicial, risco à marca."
        reasonLabel="Motivo (fica na auditoria)"
        minLength={20}
        requireText={c.name}
        confirmLabel="Interromper agora"
        onConfirm={async (reason) => {
          setCampaign(await campaignsApi.kill(c.id, reason, newRequestId()))
          toast.success('Campanha interrompida.')
        }}
      />
      <ConfirmDialog
        open={dialog === 'approve'}
        onOpenChange={(o) => !o && setDialog(null)}
        title={`Aprovar a campanha ${c.name}?`}
        description="Os anúncios e a programação desta versão serão publicados para as telas."
        confirmLabel="Aprovar"
        onConfirm={async () => {
          setCampaign(await withDescribedError(() => campaignsApi.approve(c.id, null, newRequestId())))
          toast.success('Campanha aprovada.')
        }}
      />
      <ReasonDialog
        open={dialog === 'reject'}
        onOpenChange={(o) => !o && setDialog(null)}
        destructive
        title={`Rejeitar a campanha ${c.name}?`}
        description="O anunciante recebe o motivo e pode corrigir e reenviar. Em alterações, a versão anterior continua no ar."
        reasonLabel="Motivo da rejeição (visível ao anunciante)"
        minLength={10}
        confirmLabel="Rejeitar"
        onConfirm={async (reason) => {
          setCampaign(await campaignsApi.reject(c.id, reason, null, newRequestId()))
          toast.success('Campanha rejeitada.')
        }}
      />
      <ChangeRequestDialog campaign={c} area={area} open={dialog === 'change'} onOpenChange={(o) => !o && setDialog(null)} />
    </>
  )
}

function RevisionsTable({
  revisions,
  loading,
  error,
  liveId,
  pendingId,
}: {
  revisions: Revision[] | undefined
  loading: boolean
  error: unknown
  liveId?: string
  pendingId?: string
}) {
  if (loading) return <Skeleton className="h-32 w-full" />
  if (error) return <p className="m-0 text-sm text-danger">{describeError(error)}</p>
  if (!revisions?.length) return <p className="m-0 text-sm text-text-muted">Sem versões ainda (criadas no pagamento).</p>
  return (
    <Table>
      <caption className="sr-only">Versões da campanha</caption>
      <TableHeader>
        <TableRow className="h-11 hover:bg-transparent">
          <TableHead scope="col">Versão</TableHead>
          <TableHead scope="col">Tipo</TableHead>
          <TableHead scope="col">Situação</TableHead>
          <TableHead scope="col" numeric>
            Preço
          </TableHead>
          <TableHead scope="col" numeric>
            Diferença
          </TableHead>
          <TableHead scope="col">Data</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {revisions
          .slice()
          .reverse()
          .map((r) => (
            <TableRow key={r.id}>
              <TableCell>
                {r.number}
                {r.id === liveId ? <span className="ml-2 text-xs font-semibold text-success">no ar</span> : null}
                {r.id === pendingId ? <span className="ml-2 text-xs font-semibold text-warning">pendente</span> : null}
              </TableCell>
              <TableCell>{r.kind === 'CHANGE' ? 'Alteração' : r.kind === 'RENEWAL' ? 'Renovação' : r.kind === 'INITIAL' ? 'Compra' : r.kind}</TableCell>
              <TableCell>
                {r.status}
                {r.decision_reason ? <span className="block text-xs text-text-muted">{r.decision_reason}</span> : null}
              </TableCell>
              <TableCell numeric className="tabular-nums">
                {formatCents(r.price_cents)}
              </TableCell>
              <TableCell numeric className="tabular-nums">
                {r.price_diff_cents === null ? EMPTY_VALUE : formatCents(r.price_diff_cents)}
              </TableCell>
              <TableCell>{formatDateTime(r.decided_at ?? r.created_at)}</TableCell>
            </TableRow>
          ))}
      </TableBody>
    </Table>
  )
}

function ContractedScreensCard({
  campaign,
  isStaff,
  screens,
}: {
  campaign: CampaignDetail
  isStaff: boolean
  screens: {
    data?: { source?: string; items: Array<{ friendly_id?: string | null; state?: string | null; orientation?: string | null; letterbox?: boolean; establishment_id?: string; city_key?: string | null }> }
    isLoading: boolean
    error: unknown
  }
}) {
  const rev = campaign.live_revision ?? campaign.pending_revision
  const preview = screens.data?.source === 'PREVISAO' || (!campaign.has_live_version && !screens.data?.source)
  const items = screens.data?.items ?? []
  const byState = new Map<string, number>()
  for (const s of items) if (s.state) byState.set(s.state, (byState.get(s.state) ?? 0) + 1)
  return (
    <Card>
      <CardHeader>
        <CardTitle>{preview ? 'Telas previstas pela segmentação' : 'Telas contratadas'}</CardTitle>
        <CardDescription>
          {preview
            ? `Previsão calculada agora pelo servidor${items.length ? `: ${formatNumber(items.length)} tela(s)` : ''}. As telas são congeladas no pagamento.`
            : rev
              ? `${formatNumber(rev.screens_count)} tela(s) na versão ${rev.number}`
              : 'Telas da versão vigente.'}
          {isStaff ? '' : ' · Por privacidade, mostramos o código da tela e a UF, sem nomes ou endereços de estabelecimentos.'}
        </CardDescription>
      </CardHeader>
      {campaign.targets.length === 0 && !campaign.has_live_version ? (
        <p className="m-0 text-sm text-text-muted">Defina a segmentação para ver as telas.</p>
      ) : screens.isLoading ? (
        <Skeleton className="h-24 w-full" />
      ) : screens.error || !items.length ? (
        <p className="m-0 text-sm text-text-muted">
          {screens.error && !(isApiError(screens.error) && (screens.error.isForbidden || screens.error.isNotFound))
            ? describeError(screens.error)
            : 'Nenhuma tela disponível para esta segmentação no momento.'}
        </p>
      ) : (
        <>
          <p className="m-0 text-sm">
            Por UF:{' '}
            {[...byState.entries()]
              .sort()
              .map(([uf, n]) => `${uf} (${n})`)
              .join(', ') || EMPTY_VALUE}
          </p>
          <Table>
            <caption className="sr-only">Telas contratadas</caption>
            <TableHeader>
              <TableRow className="h-11 hover:bg-transparent">
                <TableHead scope="col">Código da tela</TableHead>
                <TableHead scope="col">UF</TableHead>
                <TableHead scope="col">Orientação</TableHead>
                <TableHead scope="col">Ajuste</TableHead>
                {isStaff ? <TableHead scope="col">Cidade</TableHead> : null}
                {isStaff ? <TableHead scope="col">Estabelecimento</TableHead> : null}
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.slice(0, 200).map((s, i) => (
                <TableRow key={s.friendly_id ?? i}>
                  <TableCell className="font-mono">{s.friendly_id ?? EMPTY_VALUE}</TableCell>
                  <TableCell>{s.state ?? EMPTY_VALUE}</TableCell>
                  <TableCell>{s.orientation === 'VERTICAL' ? 'Vertical' : s.orientation === 'HORIZONTAL' ? 'Horizontal' : EMPTY_VALUE}</TableCell>
                  <TableCell>{s.letterbox ? 'Com tarja (orientação diferente)' : 'Tela cheia'}</TableCell>
                  {isStaff ? <TableCell>{cityKeyLabel(s.city_key)}</TableCell> : null}
                  {isStaff ? (
                    <TableCell>
                      <EstablishmentName id={s.establishment_id} />
                    </TableCell>
                  ) : null}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </>
      )}
    </Card>
  )
}
