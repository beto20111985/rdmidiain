/**
 * Pedido de arte — detalhe compartilhado (RD, anunciante, estabelecimento):
 * briefing e anexos, orçamento/cobrança, versões (pré-visualização protegida), 3 revisões
 * incluídas (4ª = revisão extra paga), APROVAÇÃO FORMAL do cliente (data/hora/IP/usuário
 * exibidos depois), arte aprovada TRAVADA (mudança = nova versão + nova aprovação),
 * publicação como mídia, comentários públicos × internos e histórico.
 */
import * as React from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { CheckCircle2, Download, FileUp, Lock, MessageSquareWarning, Send, Upload, UserCheck, Wand2, XCircle } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Checkbox } from '@/components/ui/checkbox'
import { Progress } from '@/components/ui/progress'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Field, FieldDescription, FieldGroup, FieldLabel } from '@/components/ui/field'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { FileDropzone, type UploadItem } from '@/components/patterns/file-dropzone'
import { ErrorState, NoPermission, PageSkeleton } from '@/components/patterns/states'
import { useCan } from '@/hooks/usePermissions'
import { useAuthStore } from '@/stores/authStore'
import { useActiveRouteContext } from '@/stores/contextStore'
import { http, newRequestId } from '@/lib/http'
import { uploadWithProgress } from '@/lib/upload'
import { qk } from '@/lib/queryKeys'
import { errorMessage, isApiError } from '@/lib/errors'
import { formatBytes, formatCents, formatDateTime } from '@/lib/format'
import { cn } from '@/lib/utils'
import {
  ATTACHMENT_KIND,
  DESIGN_BILLING,
  DESIGN_KIND,
  DESIGN_STATUS,
  DESIGN_STATUS_STAFF,
  DESIGN_VERSION_STATUS,
  EXTRA_REVISION_STATUS,
  PAYMENT_STATUS,
  labelOf,
  lookup,
} from '@/components/admin/labels'
import { KeyValueList, MobileMeta, RealtimeStale, ReasonDialog, ToneBadge } from '@/components/admin/ui'
import { BlobImage } from '@/components/admin/blob-image'
import { downloadFile } from '@/components/admin/binary'
import { parseBrlToCents } from '@/components/admin/money'
import { useAssignableUsers, useUserDirectory } from '@/components/admin/directory'
import { DESIGN_PATHS, revisionLimit } from './paths'
import {
  ATTACHMENT_ACCEPT,
  ATTACHMENT_MAX,
  VERSION_ACCEPT_IMAGE,
  VERSION_ACCEPT_VIDEO,
  attachmentProblem,
  expectedArtSize,
  inferAttachmentKind,
  readImageSize,
} from './attachments'
import type { DesignComment, DesignRequestDetail, DesignTemplateRow, DesignVersion } from './types'

const CLOSED = new Set(['CANCELADA', 'PUBLICADA'])

export default function DesignRequestDetailPage() {
  const { id = '' } = useParams()
  const ctx = useActiveRouteContext() ?? 'rd'
  const paths = DESIGN_PATHS[ctx]
  const can = useCan()
  const me = useAuthStore((s) => s.me)
  const qc = useQueryClient()
  const staff = ctx === 'rd'
  const worker = staff && can('design.work')
  const q = useQuery({ queryKey: qk.detail('design', id), queryFn: () => http.get<DesignRequestDetail>(`/design/requests/${id}`), enabled: !!id })
  const comments = useQuery({
    queryKey: qk.sub('design', 'detail', id, 'comments'),
    queryFn: () => http.get<{ items: DesignComment[] }>(`/design/requests/${id}/comments`).then((r) => r.items),
    enabled: !!id,
  })
  const directory = useUserDirectory()
  const designers = useAssignableUsers('design', 'design.work', '/design/designers', worker)
  const [approveOpen, setApproveOpen] = React.useState<DesignVersion | null>(null)
  const [changeOpen, setChangeOpen] = React.useState<DesignVersion | null>(null)
  const [reopenOpen, setReopenOpen] = React.useState(false)
  const [extraOpen, setExtraOpen] = React.useState(false)
  const [cancelOpen, setCancelOpen] = React.useState(false)
  const [publishing, setPublishing] = React.useState(false)
  const invalidate = () => void qc.invalidateQueries({ queryKey: ['design'] })

  const assign = useMutation({
    mutationFn: (designer: string | null) => http.post(`/design/requests/${id}/assign`, { designer_id: designer }),
    onSuccess: () => {
      invalidate()
      toast.success('Responsável atualizado.')
    },
    onError: (e) => toast.error(errorMessage(e)),
  })

  if (q.isLoading) return <PageSkeleton />
  if (q.error || !q.data) {
    if (isApiError(q.error) && q.error.status === 403) return <NoPermission permission="design.request" />
    return (
      <>
        <PageHeader title="Pedido de arte" breadcrumbs={[{ label: 'Design', to: paths.list }, { label: 'Pedido' }]} />
        <ErrorState error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />
      </>
    )
  }
  const r = q.data
  const statusMap = staff ? DESIGN_STATUS_STAFF : DESIGN_STATUS
  const limit = revisionLimit(r)
  const extraCount = r.extra_revisions_count
  const latest = r.versions.length ? r.versions[r.versions.length - 1] : null
  const clientCanDecide = !staff && can('design.approve')
  const awaitingDecision = r.status === 'EM_REVISAO' && latest?.status === 'EM_REVISAO'
  const openExtra = r.extra_revision_purchases.find((x) => x.status === 'SOLICITADA' || x.status === 'AGUARDANDO_PAGAMENTO')
  const approvedRecord = [...r.approvals].reverse().find((a) => a.decision === 'APROVADA' && a.version_id === r.approved_version_id) ?? null
  const approvedVersion = r.versions.find((v) => v.id === r.approved_version_id) ?? null
  const who = (uid: string) => (uid === me?.user.id ? 'Você' : directory.nameOf(uid) ?? (staff ? 'Usuário do cliente' : 'Usuário da sua empresa'))
  const canCancel = !CLOSED.has(r.status) && (worker || (can('design.request') && (r.status === 'BRIEFING' || r.status === 'AGUARDANDO_PAGAMENTO')))

  const headerMeta = (
    <span className="inline-flex flex-wrap items-center gap-2">
      <ToneBadge value={lookup(statusMap, r.status)} />
      <span>
        {labelOf(DESIGN_KIND, r.kind)} · {r.media_type === 'VIDEO' ? `vídeo ${r.duration_s}s` : 'imagem'} · {r.orientation === 'VERTICAL' ? '1080×1920' : '1920×1080'}
      </span>
    </span>
  )

  return (
    <>
      <PageHeader
        title={`${r.code} · ${r.title}`}
        description={headerMeta}
        breadcrumbs={[{ label: staff ? 'Briefings' : 'Design', to: paths.list }, { label: r.code }]}
        actions={
          canCancel ? (
            <Button variant="destructive" onClick={() => setCancelOpen(true)}>
              <XCircle aria-hidden="true" />
              Cancelar pedido
            </Button>
          ) : undefined
        }
      />
      <div className="flex flex-col gap-4">
        <MobileMeta>{headerMeta}</MobileMeta>
        <RealtimeStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />
        {r.status === 'AGUARDANDO_PAGAMENTO' ? (
          <Alert variant="warning">
            <div className="flex min-w-0 flex-1 flex-col gap-1">
              <AlertTitle>Aguardando o pagamento da criação ({formatCents(r.price_cents)})</AlertTitle>
              <AlertDescription>
                O pedido entra na fila do designer assim que o pagamento for confirmado.{' '}
                {ctx === 'advertiser' ? (
                  <Link to="/anunciante/faturas" className="font-semibold text-link underline-offset-4 hover:underline">
                    Ver faturas
                  </Link>
                ) : null}
              </AlertDescription>
            </div>
          </Alert>
        ) : null}
        {r.status === 'CANCELADA' ? (
          <Alert variant="neutral">
            <AlertDescription>Pedido cancelado. Motivo: {r.cancel_reason ?? '—'}</AlertDescription>
          </Alert>
        ) : null}

        <div className="grid grid-cols-1 gap-4 xl:grid-cols-12 xl:gap-6">
          <div className="flex flex-col gap-4 xl:col-span-8">
            {approvedRecord && approvedVersion ? (
              <Card className="border-success/60">
                <CardHeader>
                  <div className="flex flex-col gap-1">
                    <CardTitle className="flex items-center gap-2">
                      <CheckCircle2 aria-hidden="true" className="size-5 text-success" />
                      Aprovação formal registrada
                    </CardTitle>
                    <CardDescription>
                      <Lock aria-hidden="true" className="mr-1 inline size-3.5" />
                      Arte aprovada travada: qualquer alteração gera uma nova versão, que precisa de nova aprovação.
                    </CardDescription>
                  </div>
                </CardHeader>
                <KeyValueList
                  items={[
                    { label: 'Aprovada por', value: who(approvedRecord.user_id) },
                    { label: 'Data e hora', value: formatDateTime(approvedRecord.decided_at) },
                    { label: 'IP', value: approvedRecord.ip_address },
                    { label: 'Versão', value: `v${approvedVersion.number}` },
                    { label: 'Navegador', value: approvedRecord.user_agent, wide: true },
                    { label: 'Impressão digital (SHA-256)', value: <code className="break-all text-xs">{approvedRecord.version_sha256}</code>, wide: true },
                    { label: 'Comentário', value: approvedRecord.comment, wide: true },
                  ]}
                />
              </Card>
            ) : null}

            <Card>
              <CardHeader>
                <div className="flex flex-col gap-1">
                  <CardTitle>Versões da arte</CardTitle>
                  <CardDescription>
                    Revisões usadas: <strong className="text-text">{r.revisions_used} de {limit}</strong> ({r.revisions_included} incluídas
                    {extraCount ? ` + ${extraCount} extra` : ''}).
                  </CardDescription>
                </div>
              </CardHeader>
              <Progress value={limit ? Math.min(100, (r.revisions_used / limit) * 100) : 0} aria-label={`Revisões usadas: ${r.revisions_used} de ${limit}`} />
              {r.revisions_remaining === 0 && !CLOSED.has(r.status) ? (
                <Alert variant="warning">
                  <MessageSquareWarning aria-hidden="true" />
                  <div className="flex flex-col gap-1">
                    <AlertTitle>As {r.revisions_included} revisões incluídas foram usadas</AlertTitle>
                    <AlertDescription>
                      A próxima alteração ({limit + 1}ª) exige uma revisão extra, cobrada à parte e liberada pela RD.
                      {openExtra ? ` Revisão extra ${lookup(EXTRA_REVISION_STATUS, openExtra.status).label.toLowerCase()}.` : ''}
                    </AlertDescription>
                  </div>
                  {!staff && !openExtra && (can('design.request') || can('design.approve')) ? (
                    <Button variant="secondary" onClick={() => setExtraOpen(true)}>
                      Solicitar revisão extra
                    </Button>
                  ) : null}
                </Alert>
              ) : null}
              {r.versions.length ? (
                <ol className="m-0 flex list-none flex-col gap-4 p-0" reversed>
                  {[...r.versions].reverse().map((v) => (
                    <li key={v.id} className={cn('flex flex-col gap-3 rounded-lg border p-4', v.locked ? 'border-success/60' : 'border-border')}>
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="flex flex-wrap items-center gap-2 font-semibold text-text">
                          Versão {v.number}
                          <ToneBadge value={lookup(DESIGN_VERSION_STATUS, v.status)} />
                          {v.locked ? (
                            <Badge tone="success">
                              <Lock aria-hidden="true" />
                              Travada
                            </Badge>
                          ) : null}
                          {v.source === 'TEMPLATE' ? <Badge tone="outline">Gerada por template</Badge> : null}
                        </span>
                        <span className="text-xs text-text-muted">
                          {formatDateTime(v.created_at)} · {v.width && v.height ? `${v.width}×${v.height} · ` : ''}
                          {formatBytes(v.size_bytes)}
                        </span>
                      </div>
                      {v.content_type.startsWith('image/') ? (
                        <BlobImage
                          domain="design"
                          path={`/design/requests/${r.id}/versions/${v.id}/file`}
                          alt={`Versão ${v.number} da arte ${r.title}`}
                          aspect={r.orientation === 'VERTICAL' ? '9 / 16' : '16 / 9'}
                          className={r.orientation === 'VERTICAL' ? 'max-w-[320px]' : undefined}
                        />
                      ) : (
                        <p className="text-sm text-text-muted">Vídeo ({v.filename}) — baixe para assistir.</p>
                      )}
                      {v.notes ? <p className="m-0 text-sm text-text">Observações do designer: {v.notes}</p> : null}
                      <div className="flex flex-wrap gap-2">
                        <Button
                          variant="secondary"
                          onClick={() =>
                            void downloadFile(`/design/requests/${r.id}/versions/${v.id}/file`, v.filename).catch((e) => toast.error(errorMessage(e)))
                          }
                        >
                          <Download aria-hidden="true" />
                          Baixar
                        </Button>
                        {clientCanDecide && awaitingDecision && latest?.id === v.id ? (
                          <>
                            <Button onClick={() => setApproveOpen(v)}>
                              <CheckCircle2 aria-hidden="true" />
                              Aprovar formalmente
                            </Button>
                            <Button variant="secondary" onClick={() => setChangeOpen(v)}>
                              Solicitar alteração
                            </Button>
                          </>
                        ) : null}
                      </div>
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="text-sm text-text-muted">
                  {staff ? 'Nenhuma versão enviada ainda.' : 'A primeira versão aparece aqui quando o designer concluir a criação.'}
                </p>
              )}
              {(r.status === 'APROVADA' || r.status === 'PUBLICADA') && (worker || (clientCanDecide && r.revisions_remaining > 0)) ? (
                <Button variant="secondary" className="w-fit" onClick={() => setReopenOpen(true)}>
                  {worker ? 'Reabrir para correção interna' : 'Pedir alteração após a aprovação'}
                </Button>
              ) : null}
            </Card>

            {worker && !CLOSED.has(r.status) && r.status !== 'BRIEFING' && r.status !== 'AGUARDANDO_PAGAMENTO' ? <NewVersionCard request={r} onDone={invalidate} /> : null}

            <CommentsCard requestId={r.id} versions={r.versions} comments={comments.data ?? []} loading={comments.isLoading} error={comments.error} worker={worker} staffView={staff} who={who} />
          </div>

          <div className="flex flex-col gap-4 xl:col-span-4">
            <Card>
              <CardHeader>
                <CardTitle>Resumo</CardTitle>
              </CardHeader>
              <KeyValueList
                className="md:grid-cols-1"
                items={[
                  { label: 'Cobrança', value: `${labelOf(DESIGN_BILLING, r.billing)}${r.billing === 'COBRAR' ? ` · ${formatCents(r.price_cents)}` : ''}` },
                  { label: 'Pagamento', value: lookup(PAYMENT_STATUS, r.payment_status).label },
                  { label: 'Prazo desejado', value: formatDateTime(r.due_at) },
                  ...(staff ? [{ label: 'Responsável', value: r.assigned_to ? who(r.assigned_to) : 'Sem responsável' }] : []),
                  { label: 'Aberto em', value: formatDateTime(r.created_at) },
                  { label: 'Publicado em', value: formatDateTime(r.published_at) },
                ]}
              />
              {r.published_media_id && (staff || can('creatives.view') || can('media.view')) ? (
                <Button asChild variant="tertiary" className="w-fit">
                  <Link to={staff ? '/biblioteca' : '/anunciante/campanhas'}>{staff ? 'Ver na biblioteca de mídia' : 'Usar em uma campanha'}</Link>
                </Button>
              ) : null}
            </Card>
            {worker && r.status === 'BRIEFING' ? <QuoteCard requestId={r.id} onDone={invalidate} /> : null}
            {worker && !CLOSED.has(r.status) && r.status !== 'BRIEFING' && r.status !== 'AGUARDANDO_PAGAMENTO' ? (
              <Card>
                <CardHeader>
                  <CardTitle>Responsável</CardTitle>
                </CardHeader>
                <div className="flex flex-col gap-3">
                  {r.assigned_to !== me?.user.id ? (
                    <Button variant="secondary" onClick={() => assign.mutate(null)} loading={assign.isPending && assign.variables === null}>
                      <UserCheck aria-hidden="true" />
                      Assumir pedido
                    </Button>
                  ) : null}
                  {designers.data?.users.length ? (
                    <Field>
                      <FieldLabel>Atribuir a</FieldLabel>
                      <Select value={r.assigned_to ?? ''} onValueChange={(v) => assign.mutate(v)}>
                        <SelectTrigger>
                          <SelectValue placeholder="Escolha o designer" />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectGroup>
                            {designers.data.users.map((u) => (
                              <SelectItem key={u.id} value={u.id}>
                                {u.name}
                                {typeof u.load === 'number' ? ` · ${u.load} pedido(s) ativo(s)` : ''}
                              </SelectItem>
                            ))}
                          </SelectGroup>
                        </SelectContent>
                      </Select>
                    </Field>
                  ) : null}
                </div>
              </Card>
            ) : null}
            {worker && r.status === 'APROVADA' ? (
              <Card>
                <CardHeader>
                  <div className="flex flex-col gap-1">
                    <CardTitle>Publicar como mídia</CardTitle>
                    <CardDescription>A versão aprovada vai para a biblioteca da empresa pela mesma validação do upload.</CardDescription>
                  </div>
                </CardHeader>
                <Button onClick={() => setPublishing(true)}>
                  <FileUp aria-hidden="true" />
                  Publicar na biblioteca
                </Button>
              </Card>
            ) : null}
            {r.extra_revision_purchases.length ? <ExtraRevisionsCard request={r} worker={worker} onDone={invalidate} /> : null}
            <BriefingCard request={r} canUpload={!CLOSED.has(r.status) && (worker || can('design.request'))} onDone={invalidate} />
            <Card>
              <CardHeader>
                <CardTitle>Histórico</CardTitle>
              </CardHeader>
              <ol className="m-0 flex list-none flex-col gap-2 p-0 text-sm">
                {r.history.map((h, i) => (
                  <li key={i} className="flex flex-col">
                    <span className="text-text">
                      {lookup(statusMap, h.to).label}
                      {h.note ? ` — ${h.note}` : ''}
                    </span>
                    <time dateTime={h.at} className="text-xs text-text-muted">
                      {formatDateTime(h.at)}
                    </time>
                  </li>
                ))}
              </ol>
            </Card>
          </div>
        </div>
      </div>

      <ReasonDialog
        open={!!approveOpen}
        onOpenChange={(o) => !o && setApproveOpen(null)}
        title={`Aprovar formalmente a versão ${approveOpen?.number ?? ''}?`}
        description="A aprovação formal autoriza a publicação desta arte."
        consequences={[
          'Registramos a data e a hora, o seu usuário, o IP e o navegador desta aprovação.',
          'A versão aprovada fica travada: qualquer mudança depois disso gera uma nova versão, que precisa de nova aprovação.',
        ]}
        reasonLabel="Comentário (opcional)"
        minLength={0}
        maxLength={5000}
        confirmLabel="Aprovar arte"
        onConfirm={async (comment) => {
          if (!approveOpen) return
          await http.post(`/design/requests/${r.id}/versions/${approveOpen.id}/decision`, { decision: 'APROVAR', comment: comment || null })
          invalidate()
          toast.success('Arte aprovada formalmente.')
        }}
      />
      <ReasonDialog
        open={!!changeOpen}
        onOpenChange={(o) => !o && setChangeOpen(null)}
        title="Solicitar alteração"
        description={
          r.revisions_remaining > 0
            ? `Esta solicitação usa a revisão ${r.revisions_used + 1} de ${limit}.`
            : `As revisões incluídas acabaram: a próxima alteração exige revisão extra (paga).`
        }
        reasonLabel="O que deve mudar"
        minLength={3}
        maxLength={5000}
        confirmLabel="Enviar pedido de alteração"
        onConfirm={async (comment) => {
          if (!changeOpen) return
          try {
            await http.post(`/design/requests/${r.id}/versions/${changeOpen.id}/decision`, { decision: 'SOLICITAR_ALTERACAO', comment })
          } catch (e) {
            if (isApiError(e) && e.code === 'DESIGN_REVISION_LIMIT_REACHED') {
              invalidate()
              throw new Error(`${e.message} Use “Solicitar revisão extra” (cobrada à parte).`)
            }
            throw e
          }
          invalidate()
          toast.success('Pedido de alteração enviado ao designer.')
        }}
      />
      <ReasonDialog
        open={reopenOpen}
        onOpenChange={setReopenOpen}
        title={worker ? 'Reabrir a arte para correção interna?' : 'Pedir alteração após a aprovação?'}
        consequences={
          worker
            ? ['A versão aprovada continua travada; a correção gera nova versão e exige nova aprovação do cliente.', 'Não consome revisões do cliente.']
            : [`Usa a revisão ${r.revisions_used + 1} de ${limit}.`, 'A versão aprovada continua registrada; a nova versão precisará da sua aprovação.']
        }
        reasonLabel="O que deve mudar"
        minLength={3}
        maxLength={5000}
        confirmLabel="Reabrir"
        onConfirm={async (body) => {
          await http.post(`/design/requests/${r.id}/reopen`, { body })
          invalidate()
          toast.success('Pedido reaberto.')
        }}
      />
      <ReasonDialog
        open={extraOpen}
        onOpenChange={setExtraOpen}
        title="Solicitar revisão extra"
        description="A RD analisa o pedido e informa o valor; quando houver cobrança, a revisão é liberada após o pagamento."
        reasonLabel="Motivo (opcional)"
        minLength={0}
        confirmLabel="Solicitar revisão extra"
        onConfirm={async (reason) => {
          const x = await http.post<{ price_cents: number }>(`/design/requests/${r.id}/extra-revisions`, { reason: reason || null })
          invalidate()
          toast.success(x.price_cents ? `Revisão extra solicitada (valor sugerido ${formatCents(x.price_cents)}).` : 'Revisão extra solicitada.')
        }}
      />
      <ReasonDialog
        open={cancelOpen}
        onOpenChange={setCancelOpen}
        title={`Cancelar o pedido ${r.code}?`}
        consequences={worker ? ['O pedido é encerrado.'] : ['Somente antes do início da produção. Depois disso, fale com a RD pelo suporte.']}
        minLength={3}
        maxLength={500}
        confirmLabel="Cancelar pedido"
        destructive
        onConfirm={async (reason) => {
          await http.post(`/design/requests/${r.id}/cancel`, { reason })
          invalidate()
          toast.success('Pedido cancelado.')
        }}
      />
      <ReasonDialog
        open={publishing}
        onOpenChange={setPublishing}
        title="Publicar a arte aprovada na biblioteca?"
        consequences={['A versão aprovada vira uma mídia da empresa e passa pelo processamento normal.', 'O cliente é avisado da publicação.']}
        reasonLabel="Observação (opcional)"
        minLength={0}
        confirmLabel="Publicar"
        onConfirm={async () => {
          const res = await http.post<{ media?: { id: string; status: string | null } }>(`/design/requests/${r.id}/publish`)
          invalidate()
          toast.success(`Arte publicada na biblioteca${res.media?.status ? ` (mídia ${res.media.status.toLowerCase()})` : ''}.`)
        }}
      />
    </>
  )
}

function QuoteCard({ requestId, onDone }: { requestId: string; onDone: () => void }) {
  const [billing, setBilling] = React.useState<'COBRAR' | 'NEGOCIADO' | 'ISENTO'>('COBRAR')
  const [price, setPrice] = React.useState('')
  const [note, setNote] = React.useState('')
  const cents = parseBrlToCents(price)
  const m = useMutation({
    mutationFn: () => http.post(`/design/requests/${requestId}/quote`, { billing, price_cents: billing === 'COBRAR' ? cents ?? 0 : 0, note: note.trim() || null }),
    onSuccess: () => {
      onDone()
      toast.success(billing === 'COBRAR' ? 'Orçamento enviado: cobrança gerada.' : 'Pedido liberado para a fila.')
    },
  })
  const ok = billing !== 'COBRAR' || (cents ?? 0) > 0
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Orçamento da criação</CardTitle>
          <CardDescription>“Cobrar” gera a cobrança no financeiro; o pedido entra na fila após o pagamento.</CardDescription>
        </div>
      </CardHeader>
      <form
        noValidate
        className="flex flex-col gap-4"
        onSubmit={(e) => {
          e.preventDefault()
          if (ok) m.mutate()
        }}
      >
        <ToggleGroup type="single" aria-label="Forma de cobrança" value={billing} onValueChange={(v) => v && setBilling(v as typeof billing)} className="w-fit">
          <ToggleGroupItem value="COBRAR">Cobrar</ToggleGroupItem>
          <ToggleGroupItem value="NEGOCIADO">Negociado</ToggleGroupItem>
          <ToggleGroupItem value="ISENTO">Isento</ToggleGroupItem>
        </ToggleGroup>
        {billing === 'COBRAR' ? (
          <Field required>
            <FieldLabel>Valor (R$)</FieldLabel>
            <Input inputMode="decimal" value={price} onChange={(e) => setPrice(e.target.value)} />
            <FieldDescription>{cents !== null ? `= ${formatCents(cents)}` : 'Use vírgula para os centavos.'}</FieldDescription>
          </Field>
        ) : null}
        <Field>
          <FieldLabel>Observação{billing !== 'COBRAR' ? ' (justificativa)' : ''}</FieldLabel>
          <Textarea value={note} onChange={(e) => setNote(e.target.value)} maxLength={1000} />
        </Field>
        {m.error ? (
          <Alert variant="danger">
            <AlertDescription className="text-text">{errorMessage(m.error)}</AlertDescription>
          </Alert>
        ) : null}
        <Button type="submit" loading={m.isPending} disabled={!ok}>
          Definir orçamento
        </Button>
      </form>
    </Card>
  )
}

function ExtraRevisionsCard({ request, worker, onDone }: { request: DesignRequestDetail; worker: boolean; onDone: () => void }) {
  const [deciding, setDeciding] = React.useState<{ id: string; approve: boolean; price: number } | null>(null)
  const [price, setPrice] = React.useState('')
  return (
    <Card>
      <CardHeader>
        <CardTitle>Revisões extras</CardTitle>
      </CardHeader>
      <ul className="m-0 flex list-none flex-col gap-3 p-0 text-sm">
        {request.extra_revision_purchases.map((x) => (
          <li key={x.id} className="flex flex-col gap-2 rounded-md border border-border p-3">
            <span className="flex flex-wrap items-center gap-2">
              <ToneBadge value={lookup(EXTRA_REVISION_STATUS, x.status)} />
              <span className="tabular-nums">{x.price_cents ? formatCents(x.price_cents) : 'Sem custo'}</span>
            </span>
            {x.reason ? <span className="text-text-muted">{x.reason}</span> : null}
            <span className="text-xs text-text-muted">Pedida em {formatDateTime(x.created_at)}</span>
            {worker && x.status === 'SOLICITADA' ? (
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="secondary"
                  onClick={() => {
                    setPrice(x.price_cents ? String(x.price_cents / 100).replace('.', ',') : '')
                    setDeciding({ id: x.id, approve: true, price: x.price_cents })
                  }}
                >
                  Aprovar
                </Button>
                <Button variant="ghost" onClick={() => setDeciding({ id: x.id, approve: false, price: 0 })}>
                  Recusar
                </Button>
              </div>
            ) : null}
          </li>
        ))}
      </ul>
      <ReasonDialog
        open={!!deciding}
        onOpenChange={(o) => !o && setDeciding(null)}
        title={deciding?.approve ? 'Aprovar revisão extra' : 'Recusar revisão extra'}
        description={deciding?.approve ? 'Valor > 0 gera cobrança e libera a revisão após o pagamento; valor 0 exige justificativa.' : undefined}
        reasonLabel="Observação"
        minLength={0}
        confirmLabel={deciding?.approve ? 'Aprovar' : 'Recusar'}
        destructive={!deciding?.approve}
        onConfirm={async (note) => {
          if (!deciding) return
          const cents = deciding.approve ? parseBrlToCents(price) ?? 0 : undefined
          if (deciding.approve && cents === 0 && !note) throw new Error('Justifique a revisão extra sem custo.')
          await http.post(`/design/extra-revisions/${deciding.id}/decision`, { approve: deciding.approve, price_cents: cents, note: note || null })
          onDone()
          toast.success(deciding.approve ? 'Revisão extra aprovada.' : 'Revisão extra recusada.')
        }}
      >
        {deciding?.approve ? (
          <Field>
            <FieldLabel>Valor (R$)</FieldLabel>
            <Input inputMode="decimal" value={price} onChange={(e) => setPrice(e.target.value)} />
            <FieldDescription>{parseBrlToCents(price) !== null ? `= ${formatCents(parseBrlToCents(price))}` : 'Vazio = sem custo.'}</FieldDescription>
          </Field>
        ) : null}
      </ReasonDialog>
    </Card>
  )
}

function NewVersionCard({ request, onDone }: { request: DesignRequestDetail; onDone: () => void }) {
  const [items, setItems] = React.useState<UploadItem[]>([])
  const [notes, setNotes] = React.useState('')
  const [pending, setPending] = React.useState(false)
  const [templateOpen, setTemplateOpen] = React.useState(false)
  const size = expectedArtSize(request.orientation)
  const isImage = request.media_type === 'IMAGEM'

  const add = async (files: File[]) => {
    const f = files[0]
    if (!f) return
    let problem: string | null = null
    let meta: string | undefined
    if (isImage) {
      const dim = await readImageSize(f)
      if (dim) {
        meta = `${dim.width}×${dim.height}`
        if (dim.width !== size.width || dim.height !== size.height) problem = `A arte deve ter exatamente ${size.width}×${size.height} px.`
      }
    }
    setItems([{ id: newRequestId(), file: f, status: problem ? 'error' : 'selected', error: problem ?? undefined, meta }])
  }
  const send = async () => {
    const it = items[0]
    if (!it || it.status === 'error') return
    setPending(true)
    setItems([{ ...it, status: 'uploading', progress: 0 }])
    try {
      const form = new FormData()
      form.append('file', it.file)
      if (notes.trim()) form.append('notes', notes.trim())
      await uploadWithProgress({
        path: `/design/requests/${request.id}/versions`,
        form,
        onProgress: (p) => setItems((prev) => prev.map((x) => ({ ...x, progress: p }))),
      })
      setItems([])
      setNotes('')
      onDone()
      toast.success('Nova versão enviada ao cliente para revisão.')
    } catch (e) {
      setItems([{ ...it, status: 'error', error: errorMessage(e) }])
    } finally {
      setPending(false)
    }
  }
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Enviar nova versão</CardTitle>
          <CardDescription>
            {isImage ? `PNG ou JPG em ${size.width}×${size.height} px, até 10 MB.` : 'Vídeo MP4 até 200 MB.'} Versões em revisão anteriores são substituídas;
            a aprovada continua travada.
          </CardDescription>
        </div>
        {isImage ? (
          <Button variant="secondary" onClick={() => setTemplateOpen(true)}>
            <Wand2 aria-hidden="true" />
            Gerar por template
          </Button>
        ) : null}
      </CardHeader>
      <FileDropzone
        accept={isImage ? VERSION_ACCEPT_IMAGE : VERSION_ACCEPT_VIDEO}
        maxSizeBytes={isImage ? 10 * 1024 * 1024 : 200 * 1024 * 1024}
        multiple={false}
        requirements={isImage ? `PNG/JPG ${size.width}×${size.height}` : 'MP4'}
        items={items}
        onFilesAccepted={(f) => void add(f)}
        onRemove={() => setItems([])}
        disabled={pending}
        label="Arquivo da versão"
      />
      <Field>
        <FieldLabel>Observações para o cliente</FieldLabel>
        <Textarea value={notes} maxLength={2000} onChange={(e) => setNotes(e.target.value)} />
      </Field>
      <Button className="w-fit" onClick={() => void send()} loading={pending} disabled={!items.length || items[0].status === 'error'}>
        <Upload aria-hidden="true" />
        Enviar versão
      </Button>
      <FromTemplateDialog open={templateOpen} onOpenChange={setTemplateOpen} request={request} onDone={onDone} />
    </Card>
  )
}

interface SpecVariable {
  name: string
  label: string
  max_len?: number
  default?: string | null
}

function FromTemplateDialog({ open, onOpenChange, request, onDone }: { open: boolean; onOpenChange: (o: boolean) => void; request: DesignRequestDetail; onDone: () => void }) {
  const templates = useQuery({
    queryKey: ['design', 'templates', { orientation: request.orientation, status: 'ATIVO' }],
    queryFn: () => http.get<{ items: DesignTemplateRow[] }>('/templates', { query: { orientation: request.orientation, status: 'ATIVO' } }).then((r) => r.items),
    enabled: open,
  })
  const [tid, setTid] = React.useState('')
  const [values, setValues] = React.useState<Record<string, string>>({})
  const [format, setFormat] = React.useState<'PNG' | 'JPEG'>('PNG')
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)
  const tpl = templates.data?.find((t) => t.id === tid)
  const vars: SpecVariable[] = ((tpl?.spec as { variables?: SpecVariable[] } | undefined)?.variables ?? []) as SpecVariable[]
  React.useEffect(() => {
    if (!open) {
      setTid('')
      setValues({})
      setError(null)
    }
  }, [open])
  return (
    <Dialog open={open} onOpenChange={(o) => (!pending ? onOpenChange(o) : undefined)}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>Gerar versão por template</DialogTitle>
          <DialogDescription>A arte é renderizada no servidor ({request.orientation === 'VERTICAL' ? '1080×1920' : '1920×1080'}) e entra como nova versão para revisão.</DialogDescription>
        </DialogHeader>
        <FieldGroup>
          <Field>
            <FieldLabel>Template</FieldLabel>
            <Select value={tid} onValueChange={setTid}>
              <SelectTrigger>
                <SelectValue placeholder={templates.isLoading ? 'Carregando…' : templates.data?.length ? 'Selecione' : 'Nenhum template ativo nesta orientação'} />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  {(templates.data ?? []).map((t) => (
                    <SelectItem key={t.id} value={t.id}>
                      {t.name}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
          </Field>
          {vars.map((v) => (
            <Field key={v.name}>
              <FieldLabel>{v.label}</FieldLabel>
              <Input value={values[v.name] ?? v.default ?? ''} maxLength={v.max_len ?? 300} onChange={(e) => setValues((p) => ({ ...p, [v.name]: e.target.value }))} />
            </Field>
          ))}
          <ToggleGroup type="single" aria-label="Formato" value={format} onValueChange={(v) => v && setFormat(v as 'PNG' | 'JPEG')} className="w-fit">
            <ToggleGroupItem value="PNG">PNG</ToggleGroupItem>
            <ToggleGroupItem value="JPEG">JPG</ToggleGroupItem>
          </ToggleGroup>
        </FieldGroup>
        {error ? (
          <Alert variant="danger">
            <AlertDescription className="text-text">{error}</AlertDescription>
          </Alert>
        ) : null}
        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)} disabled={pending}>
            Cancelar
          </Button>
          <Button
            loading={pending}
            disabled={!tid}
            onClick={async () => {
              setPending(true)
              setError(null)
              try {
                const variables = Object.fromEntries(vars.map((v) => [v.name, values[v.name] ?? v.default ?? '']))
                await http.post(`/design/requests/${request.id}/versions/from-template`, { template_id: tid, variables, format })
                onDone()
                toast.success('Versão gerada pelo template e enviada para revisão.')
                onOpenChange(false)
              } catch (e) {
                setError(errorMessage(e))
              } finally {
                setPending(false)
              }
            }}
          >
            Gerar versão
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function BriefingCard({ request, canUpload, onDone }: { request: DesignRequestDetail; canUpload: boolean; onDone: () => void }) {
  const [items, setItems] = React.useState<UploadItem[]>([])
  const upload = async (files: File[]) => {
    for (const f of files) {
      const problem = attachmentProblem(f)
      const id = newRequestId()
      setItems((prev) => [...prev, { id, file: f, status: problem ? 'error' : 'uploading', error: problem ?? undefined, progress: 0 }])
      if (problem) continue
      try {
        const form = new FormData()
        form.append('kind', inferAttachmentKind(f))
        form.append('file', f)
        await uploadWithProgress({
          path: `/design/requests/${request.id}/attachments`,
          form,
          onProgress: (p) => setItems((prev) => prev.map((x) => (x.id === id ? { ...x, progress: p } : x))),
        })
        setItems((prev) => prev.map((x) => (x.id === id ? { ...x, status: 'ready', progress: 1 } : x)))
        onDone()
      } catch (e) {
        setItems((prev) => prev.map((x) => (x.id === id ? { ...x, status: 'error', error: errorMessage(e) } : x)))
      }
    }
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle>Briefing</CardTitle>
      </CardHeader>
      <p className="m-0 whitespace-pre-line text-sm text-text">{request.briefing}</p>
      {request.texts.length ? (
        <div className="flex flex-col gap-1">
          <h3 className="text-xs font-semibold text-text-muted">Textos</h3>
          <ul className="m-0 flex list-disc flex-col gap-0.5 pl-5 text-sm">
            {request.texts.map((t, i) => (
              <li key={i}>{t}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {request.references.length ? (
        <div className="flex flex-col gap-1">
          <h3 className="text-xs font-semibold text-text-muted">Referências</h3>
          <ul className="m-0 flex list-disc flex-col gap-0.5 break-all pl-5 text-sm">
            {request.references.map((t, i) => (
              <li key={i}>{t}</li>
            ))}
          </ul>
        </div>
      ) : null}
      <div className="flex flex-col gap-2">
        <h3 className="text-xs font-semibold text-text-muted">Anexos</h3>
        {request.attachments.length ? (
          <ul className="m-0 flex list-none flex-col gap-1 p-0 text-sm">
            {request.attachments.map((a) => (
              <li key={a.id} className="flex items-center justify-between gap-2">
                <span className="min-w-0 truncate">
                  {a.filename} <span className="text-xs text-text-muted">· {labelOf(ATTACHMENT_KIND, a.kind)} · {formatBytes(a.size_bytes)}</span>
                </span>
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label={`Baixar ${a.filename}`}
                  onClick={() => void downloadFile(`/design/requests/${request.id}/attachments/${a.id}/file`, a.filename).catch((e) => toast.error(errorMessage(e)))}
                >
                  <Download aria-hidden="true" />
                </Button>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-text-muted">Nenhum anexo.</p>
        )}
      </div>
      {canUpload ? (
        <FileDropzone
          accept={ATTACHMENT_ACCEPT}
          maxSizeBytes={ATTACHMENT_MAX}
          requirements="Imagens até 10 MB, PDF até 20 MB, vídeos até 200 MB."
          items={items}
          onFilesAccepted={(f) => void upload(f)}
          onRemove={(id) => setItems((prev) => prev.filter((x) => x.id !== id))}
          label="Anexar ao briefing"
        />
      ) : null}
    </Card>
  )
}

function CommentsCard({
  requestId,
  versions,
  comments,
  loading,
  error,
  worker,
  staffView,
  who,
}: {
  requestId: string
  versions: DesignVersion[]
  comments: DesignComment[]
  loading: boolean
  error: unknown
  worker: boolean
  staffView: boolean
  who: (id: string) => string
}) {
  const qc = useQueryClient()
  const [body, setBody] = React.useState('')
  const [internal, setInternal] = React.useState(false)
  const [versionId, setVersionId] = React.useState('none')
  const send = useMutation({
    mutationFn: () =>
      http.post(`/design/requests/${requestId}/comments`, { body: body.trim(), is_internal: worker && internal, version_id: versionId === 'none' ? null : versionId }),
    onSuccess: () => {
      setBody('')
      setInternal(false)
      void qc.invalidateQueries({ queryKey: ['design', 'detail', requestId, 'comments'] })
      toast.success('Comentário registrado.')
    },
  })
  // Defesa extra: fora da equipe RD nenhum comentário interno é exibido.
  const visible = staffView ? comments : comments.filter((c) => !c.is_internal)
  // Nome do autor vem do servidor (`author_name`; o cliente recebe "Equipe RD" e sem id).
  const author = (c: DesignComment) => {
    if (c.author_id && who(c.author_id) === 'Você') return 'Você'
    if (c.author_name) return staffView && c.author_is_staff ? `${c.author_name} (Equipe RD)` : c.author_name
    if (c.author_id) return who(c.author_id)
    return c.author_is_staff ? 'Equipe RD' : 'Participante'
  }
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Comentários</CardTitle>
          {worker ? <CardDescription>Comentários internos ficam só com a equipe RD.</CardDescription> : null}
        </div>
      </CardHeader>
      {loading ? (
        <PageSkeleton label="Carregando comentários…" />
      ) : error ? (
        <ErrorState error={error} size="compact" />
      ) : visible.length ? (
        <ol className="m-0 flex list-none flex-col gap-3 p-0" aria-label="Comentários do pedido">
          {visible.map((c) => (
            <li key={c.id} className={cn('flex flex-col gap-1 rounded-lg border p-3', c.is_internal ? 'border-warning/60 bg-warning-soft' : 'border-border')}>
              <span className="flex flex-wrap items-center gap-2 text-xs text-text-muted">
                <strong className="text-sm text-text">{author(c)}</strong>
                <time dateTime={c.created_at}>{formatDateTime(c.created_at)}</time>
                {c.version_id ? <span>· versão {versions.find((v) => v.id === c.version_id)?.number ?? '—'}</span> : null}
                {c.is_internal ? (
                  <Badge tone="warning">
                    <Lock aria-hidden="true" />
                    Interno
                  </Badge>
                ) : null}
              </span>
              <p className="m-0 whitespace-pre-line text-sm text-text">{c.body}</p>
            </li>
          ))}
        </ol>
      ) : (
        <p className="text-sm text-text-muted">Nenhum comentário ainda.</p>
      )}
      <form
        noValidate
        className="flex flex-col gap-3 border-t border-border pt-4"
        onSubmit={(e) => {
          e.preventDefault()
          if (body.trim()) send.mutate()
        }}
      >
        <Field>
          <FieldLabel>Novo comentário</FieldLabel>
          <Textarea value={body} maxLength={5000} onChange={(e) => setBody(e.target.value)} />
        </Field>
        <div className="flex flex-wrap items-end gap-3">
          {versions.length ? (
            <Field className="w-full sm:w-56">
              <FieldLabel>Sobre a versão</FieldLabel>
              <Select value={versionId} onValueChange={setVersionId}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    <SelectItem value="none">Pedido em geral</SelectItem>
                    {versions.map((v) => (
                      <SelectItem key={v.id} value={v.id}>
                        Versão {v.number}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
            </Field>
          ) : null}
          {worker ? (
            <Field orientation="horizontal" className="min-h-11 w-auto">
              <Checkbox checked={internal} onCheckedChange={(c) => setInternal(c === true)} />
              <FieldLabel className="font-normal">Comentário interno (só equipe RD)</FieldLabel>
            </Field>
          ) : null}
        </div>
        {send.error ? (
          <Alert variant="danger">
            <AlertDescription className="text-text">{errorMessage(send.error)}</AlertDescription>
          </Alert>
        ) : null}
        <Button type="submit" className="w-fit" loading={send.isPending} disabled={!body.trim()}>
          <Send aria-hidden="true" />
          Comentar
        </Button>
      </form>
    </Card>
  )
}
