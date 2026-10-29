/**
 * Detalhe de um pedido de aprovação financeira (W-FIN `financial/approvals/{id}`), nos dois modos:
 * - DUPLA: o solicitante NUNCA vê "Aprovar" (só "Cancelar solicitação"); o outro Admin Master revisa o
 *   conteúdo (payload + SHA-256) e aprova com TOTP + senha financeira + código por e-mail, ou rejeita com motivo.
 * - ÚNICA (AÇÃO 003): o próprio solicitante AUTORIZA (`/authorize`) com os mesmos três fatores; não há
 *   rejeição (o solicitante cancela) e nenhuma menção a outro Master.
 * - `is_mode_change`: alerta forte — a aprovação muda o modo de aprovação da instância.
 * O servidor revalida tudo (403/409).
 */
import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { ShieldAlert, XCircle } from 'lucide-react'
import { Sheet, SheetBody, SheetContent, SheetDescription, SheetFooter, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Field, FieldError, FieldLabel } from '@/components/ui/field'
import { Textarea } from '@/components/ui/textarea'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Separator } from '@/components/ui/separator'
import { Skeleton } from '@/components/ui/skeleton'
import { ConfirmDialog } from '@/components/patterns/confirm-dialog'
import { ErrorState } from '@/components/patterns/states'
import { DetailList, type DetailItem } from '@/components/finance/detail-list'
import { StatusPill } from '@/components/finance/status-pill'
import { RequestFactorsForm } from '@/components/finance/request-factors-form'
import { APPROVAL_MODE_LABEL, approvalModeNoun } from '@/components/finance/approval-utils'
import { APPROVAL_STATUS, EXECUTION_STATE, PAYLOAD_FIELD_LABEL, pixKeyTypeLabel } from '@/components/finance/labels'
import { personLabel } from '@/components/finance/names'
import { qk } from '@/lib/queryKeys'
import { errorMessage } from '@/lib/errors'
import { formatBps, formatCents, formatDateTime, formatNumber, EMPTY_VALUE } from '@/lib/format'
import { formatIsoDate } from '@/components/finance/period'
import { financeApi, type ApprovalRequest } from './api'

const EVENT_LABEL: Record<string, string> = {
  CREATED: 'Solicitado',
  EMAIL_CODE_SENT: 'Código por e-mail enviado',
  APPROVED: 'Aprovado',
  REJECTED: 'Rejeitado',
  CANCELLED: 'Cancelado pelo solicitante',
  SELF_AUTHORIZED: 'Autorizado pelo solicitante',
  CANCELLED_MODE_CHANGE: 'Cancelado pela mudança do modo de aprovação',
  EXPIRED: 'Expirado',
  EXECUTED: 'Executado',
  EXECUTION_FAILED: 'Falha na execução',
}

function renderPayloadValue(key: string, value: unknown): React.ReactNode {
  if (value === null || value === undefined || value === '') return EMPTY_VALUE
  if (typeof value === 'boolean') return value ? 'Sim' : 'Não'
  if (key.endsWith('_cents') && typeof value === 'number') return formatCents(value)
  if (key.endsWith('_bps') && typeof value === 'number') return formatBps(value)
  if (key === 'weight_milli' && typeof value === 'number') return formatNumber(value / 1000, { maximumFractionDigits: 3 })
  if (key === 'pix_key_type' && typeof value === 'string') return pixKeyTypeLabel(value)
  if ((key === 'target_mode' || key === 'from_mode') && (value === 'SINGLE' || value === 'DUAL')) return `Aprovação ${APPROVAL_MODE_LABEL[value].toLowerCase()}`
  if ((key.endsWith('_date') || key === 'valid_from' || key === 'valid_to') && typeof value === 'string') {
    return /^\d{4}-\d{2}-\d{2}$/.test(value) ? formatIsoDate(value) : formatDateTime(value)
  }
  if (key === 'legs' && Array.isArray(value)) {
    return (
      <ul className="m-0 flex list-none flex-col gap-1 p-0">
        {value.map((leg, i) => {
          const l = leg as { account_code?: string; amount_cents?: number; memo?: string | null }
          return (
            <li key={i} className="flex flex-wrap justify-between gap-2 tabular-nums">
              <span className="font-mono text-xs">{l.account_code}</span>
              <span>
                {typeof l.amount_cents === 'number' ? formatCents(l.amount_cents) : EMPTY_VALUE}
                {l.amount_cents !== undefined ? (l.amount_cents > 0 ? ' (débito)' : ' (crédito)') : ''}
              </span>
            </li>
          )
        })}
      </ul>
    )
  }
  if (typeof value === 'object') {
    return (
      <span className="font-mono text-xs">
        {Object.entries(value as Record<string, unknown>)
          .map(([k, v]) => `${PAYLOAD_FIELD_LABEL[k] ?? k}: ${k.endsWith('_cents') && typeof v === 'number' ? formatCents(v) : String(v ?? EMPTY_VALUE)}`)
          .join(' · ')}
      </span>
    )
  }
  return String(value)
}

function payloadItems(payload: Record<string, unknown>): DetailItem[] {
  return Object.entries(payload).map(([k, v]) => ({
    label: PAYLOAD_FIELD_LABEL[k] ?? k,
    value: renderPayloadValue(k, v),
    numeric: k.endsWith('_cents'),
    wide: k === 'legs' || k === 'description' || k === 'withdrawal_resolution',
  }))
}

function RejectForm({ request, onDone }: { request: ApprovalRequest; onDone: () => void }) {
  const qc = useQueryClient()
  const [reason, setReason] = React.useState('')
  const [error, setError] = React.useState<string | null>(null)
  const [confirm, setConfirm] = React.useState(false)
  const reject = useMutation({ mutationFn: () => financeApi.reject(request.id, reason.trim()) })

  const open = () => {
    if (reason.trim().length < 5) {
      setError('Informe o motivo da rejeição (mínimo 5 caracteres).')
      return
    }
    setError(null)
    setConfirm(true)
  }

  return (
    <div className="flex flex-col gap-3">
      <h3 className="text-title-card text-text">Rejeitar</h3>
      <Field invalid={!!error}>
        <FieldLabel required>Motivo da rejeição</FieldLabel>
        <Textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={3} maxLength={2000} />
        <FieldError>{error}</FieldError>
      </Field>
      <Button variant="destructive" onClick={open} className="self-start">
        <XCircle aria-hidden="true" />
        Rejeitar pedido
      </Button>
      <ConfirmDialog
        open={confirm}
        onOpenChange={setConfirm}
        destructive
        title="Rejeitar este pedido?"
        description={`${request.action_label} — ${request.amount_cents !== null ? formatCents(request.amount_cents) : 'sem valor'}.`}
        consequences={['O pedido é encerrado e nada é executado.', 'O solicitante é notificado com o motivo.']}
        confirmLabel="Rejeitar pedido"
        onConfirm={async () => {
          await reject.mutateAsync()
          toast.success('Pedido rejeitado.')
          await qc.invalidateQueries({ queryKey: ['finance'] })
          onDone()
        }}
      />
    </div>
  )
}

// ---------------------------------------------------------------------------
export interface ApprovalDetailSheetProps {
  requestId: string | null
  onOpenChange: (open: boolean) => void
  meId: string | undefined
  /** 2FA + senha financeira configurados (habilita o formulário). */
  ready: boolean
}

export function ApprovalDetailSheet({ requestId, onOpenChange, meId, ready }: ApprovalDetailSheetProps) {
  const qc = useQueryClient()
  const open = !!requestId
  const detail = useQuery({
    queryKey: qk.sub('finance', 'approvals', 'detail', requestId ?? ''),
    queryFn: () => financeApi.approval(requestId as string),
    enabled: open,
  })
  const events = useQuery({
    queryKey: qk.sub('finance', 'approvals', 'events', requestId ?? ''),
    queryFn: () => financeApi.approvalEvents(requestId as string),
    enabled: open,
  })
  const [confirmCancel, setConfirmCancel] = React.useState(false)
  const r = detail.data
  // Nomes vêm resolvidos no pedido; eventos trazem só o id do ator → mapeia pelos participantes do pedido.
  const nameOf = (id: string | null | undefined) => {
    if (!id) return '—'
    const known = r ? ({ [r.requested_by]: r.requested_by_name, ...(r.approved_by ? { [r.approved_by]: r.approved_by_name } : {}), ...(r.rejected_by ? { [r.rejected_by]: r.rejected_by_name } : {}) } as Record<string, string | null | undefined>) : {}
    return personLabel(known[id], id, meId)
  }
  const isRequester = !!r && !!meId && r.requested_by === meId
  const pending = r?.status === 'PENDING'
  // Sem `approval_mode` (servidor/fixture anterior à AÇÃO 003) = dupla aprovação.
  const mode = r?.approval_mode ?? 'DUAL'
  const isSingle = mode === 'SINGLE'
  const canAuthorize = !!r && pending && isSingle && isRequester && r.can_authorize !== false
  const canDecide = !!r && pending && !isSingle && !isRequester && r.can_approve

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="sm:max-w-[560px]">
        <SheetHeader>
          <SheetTitle>{r ? r.action_label.charAt(0).toUpperCase() + r.action_label.slice(1) : 'Pedido de aprovação'}</SheetTitle>
          <SheetDescription>
            {isSingle
              ? 'Aprovação única: você autoriza com TOTP, senha financeira e código por e-mail. Revise o conteúdo antes de autorizar.'
              : 'Dupla aprovação: quem solicita não aprova. Revise o conteúdo antes de decidir.'}
          </SheetDescription>
        </SheetHeader>
        <SheetBody className="flex flex-col gap-6 pb-6">
          {detail.isLoading ? (
            <div role="status" className="flex flex-col gap-3">
              <span className="sr-only">Carregando pedido…</span>
              <Skeleton className="h-24 w-full" />
              <Skeleton className="h-40 w-full" />
            </div>
          ) : detail.error ? (
            <ErrorState error={detail.error} onRetry={() => void detail.refetch()} size="compact" />
          ) : r ? (
            <>
              {r.is_mode_change ? (
                <Alert variant="danger" data-testid="mode-change-alert">
                  <ShieldAlert aria-hidden="true" />
                  <div className="flex min-w-0 flex-1 flex-col gap-1">
                    <AlertTitle>Esta aprovação altera o modo de aprovação financeira da instância</AlertTitle>
                    <AlertDescription className="text-text">
                      {typeof r.payload.target_mode === 'string' && typeof r.payload.from_mode === 'string'
                        ? `Mudança de ${renderPayloadValue('from_mode', r.payload.from_mode)} para ${renderPayloadValue('target_mode', r.payload.target_mode)}. `
                        : ''}
                      {r.payload.target_mode === 'SINGLE'
                        ? 'Depois da aprovação, um único Admin Master passará a autorizar as operações financeiras, sem segunda pessoa. Os pedidos pendentes serão cancelados.'
                        : 'Depois da aprovação, as operações financeiras passarão a exigir dois Administradores Master. Os pedidos pendentes serão cancelados.'}
                    </AlertDescription>
                  </div>
                </Alert>
              ) : null}
              <div className="flex flex-wrap items-center gap-2">
                <StatusPill map={APPROVAL_STATUS} value={r.status} />
                {r.status === 'APPROVED' ? <StatusPill map={EXECUTION_STATE} value={r.execution_state} /> : null}
                {isSingle ? <Badge tone="outline">{approvalModeNoun('SINGLE')}</Badge> : null}
              </div>
              <DetailList
                items={[
                  { label: 'Valor', value: r.amount_cents !== null ? formatCents(r.amount_cents) : EMPTY_VALUE, numeric: true },
                  { label: 'Solicitado por', value: nameOf(r.requested_by) },
                  { label: 'Solicitado em', value: formatDateTime(r.requested_at) },
                  { label: 'Expira em', value: formatDateTime(r.expires_at) },
                  { label: 'Motivo', value: r.reason, wide: true },
                  ...(r.approved_by ? [{ label: 'Aprovado por', value: `${nameOf(r.approved_by)} · ${formatDateTime(r.approved_at)}` }] : []),
                  ...(r.self_authorized_by
                    ? [{ label: 'Autorizado por', value: `${personLabel(r.self_authorized_by_name, r.self_authorized_by, meId)} · ${formatDateTime(r.self_authorized_at)}` }]
                    : []),
                  ...(r.rejected_by ? [{ label: 'Rejeitado por', value: nameOf(r.rejected_by) }] : []),
                  ...(r.rejection_reason ? [{ label: 'Motivo da rejeição', value: r.rejection_reason, wide: true }] : []),
                  ...(r.execution_error ? [{ label: 'Erro na execução', value: r.execution_error, wide: true }] : []),
                  ...(r.execution_ref ? [{ label: 'Referência da execução', value: <span className="font-mono text-xs">{r.execution_ref}</span>, wide: true }] : []),
                ]}
              />
              <Separator />
              <section aria-labelledby="payload-title" className="flex flex-col gap-3">
                <h3 id="payload-title" className="text-title-card text-text">
                  Conteúdo do pedido
                </h3>
                {Object.keys(r.payload).length ? <DetailList items={payloadItems(r.payload)} /> : <p className="text-sm text-text-muted">Sem detalhes adicionais.</p>}
                <p className="m-0 break-all text-xs text-text-muted">
                  SHA-256 do conteúdo: <span className="font-mono">{r.payload_sha256}</span>
                </p>
              </section>
              <Separator />
              {pending && isRequester && !isSingle ? (
                <Alert variant="info">
                  <div className="flex min-w-0 flex-1 flex-col gap-1">
                    <AlertTitle>Você é o solicitante</AlertTitle>
                    <AlertDescription>
                      A aprovação cabe ao outro Admin Master. Enquanto estiver pendente, você pode cancelar a solicitação.
                    </AlertDescription>
                  </div>
                </Alert>
              ) : null}
              {canAuthorize ? (
                <>
                  <RequestFactorsForm request={r} kind="authorize" ready={ready} onDone={() => onOpenChange(false)} />
                  <Separator />
                  <p className="m-0 text-sm text-text-muted">Se não for fazer esta operação, cancele a solicitação abaixo. O pedido também expira sozinho.</p>
                </>
              ) : null}
              {canDecide ? (
                <>
                  <RequestFactorsForm request={r} kind="approve" ready={ready} onDone={() => onOpenChange(false)} />
                  <Separator />
                  <RejectForm request={r} onDone={() => onOpenChange(false)} />
                </>
              ) : null}
              {pending && !isRequester && !canDecide ? (
                <Alert variant="warning">
                  <AlertDescription>
                    {isSingle ? 'Somente quem solicitou pode autorizar este pedido.' : 'Este pedido não pode ser decidido pela sua conta.'}
                  </AlertDescription>
                </Alert>
              ) : null}
              <section aria-labelledby="events-title" className="flex flex-col gap-2">
                <h3 id="events-title" className="text-title-card text-text">
                  Histórico
                </h3>
                {events.isLoading ? (
                  <Skeleton className="h-16 w-full" />
                ) : events.error ? (
                  <p className="text-sm text-danger">{errorMessage(events.error)}</p>
                ) : (
                  <ol className="m-0 flex list-none flex-col gap-2 p-0">
                    {(events.data?.items ?? []).map((ev) => (
                      <li key={ev.id} className="flex flex-wrap justify-between gap-2 text-sm">
                        <span>
                          {EVENT_LABEL[ev.event] ?? ev.event}
                          {ev.actor_user_id ? ` · ${nameOf(ev.actor_user_id)}` : ' · sistema'}
                        </span>
                        <time className="tabular-nums text-text-muted" dateTime={ev.created_at}>
                          {formatDateTime(ev.created_at)}
                        </time>
                      </li>
                    ))}
                  </ol>
                )}
              </section>
            </>
          ) : null}
        </SheetBody>
        {r && pending && isRequester ? (
          <SheetFooter>
            <Button variant="destructive" onClick={() => setConfirmCancel(true)}>
              Cancelar solicitação
            </Button>
          </SheetFooter>
        ) : null}
        <ConfirmDialog
          open={confirmCancel}
          onOpenChange={setConfirmCancel}
          destructive
          title="Cancelar esta solicitação?"
          description={
            isSingle
              ? 'O pedido deixa de aguardar a sua autorização. Para executar a operação será preciso solicitar de novo.'
              : 'O pedido deixa de aguardar aprovação. Para executar a operação será preciso solicitar de novo.'
          }
          confirmLabel="Cancelar solicitação"
          cancelLabel="Voltar"
          onConfirm={async () => {
            if (!r) return
            await financeApi.cancelApproval(r.id)
            toast.success('Solicitação cancelada.')
            await qc.invalidateQueries({ queryKey: ['finance'] })
            onOpenChange(false)
          }}
        />
      </SheetContent>
    </Sheet>
  )
}
