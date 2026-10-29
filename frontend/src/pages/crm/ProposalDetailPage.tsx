/**
 * CRM → Proposta (detalhe e fluxo): rascunho (editar, concluir) → desconto acima do limite
 * aguarda aprovação de um gestor (o VENDEDOR NÃO aprova o próprio desconto) → aprovada →
 * enviada ao cliente por e-mail com o PDF (validade de 30 dias a partir do envio) → resposta
 * do cliente → conversão em pedido pelo checkout real. PDF disponível em qualquer etapa.
 */
import * as React from 'react'
import { useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { CheckCircle2, Download, Mail, Pencil, Send, ShoppingCart, XCircle } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Table, TableBody, TableCell, TableFooter, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ErrorState, NoPermission, PageSkeleton } from '@/components/patterns/states'
import { DetailList } from '@/components/finance/detail-list'
import { StatusPill } from '@/components/finance/status-pill'
import { PROPOSAL_STATUS } from '@/components/finance/labels'
import { formatIsoDate } from '@/components/finance/period'
import { personLabel } from '@/components/finance/names'
import { useDownload } from '@/components/finance/use-download'
import { ReasonConfirmDialog } from '@/components/finance/reason-confirm-dialog'
import { useCan } from '@/hooks/usePermissions'
import { useAuthStore } from '@/stores/authStore'
import { qk } from '@/lib/queryKeys'
import { errorMessage, isApiError } from '@/lib/errors'
import { EMPTY_VALUE, formatBps, formatCents, formatDateTime, formatNumber } from '@/lib/format'
import { crmApi, type Proposal } from './api'
import { ProposalForm } from './proposal-form'
import { ConvertDialog } from './convert-dialog'

const TERMINAL = ['CONVERTIDA', 'CANCELADA', 'RECUSADA', 'EXPIRADA']

function DiscountDecision({ p, isSeller }: { p: Proposal; isSeller: boolean }) {
  const qc = useQueryClient()
  const can = useCan()
  const [reason, setReason] = React.useState('')
  const [error, setError] = React.useState<string | null>(null)
  const decide = useMutation({
    mutationFn: (approve: boolean) => crmApi.discountDecision(p.id, approve, reason.trim() || undefined),
    onSuccess: async (_r, approve) => {
      toast.success(approve ? 'Desconto aprovado.' : 'Desconto recusado; a proposta voltou para rascunho.')
      await qc.invalidateQueries({ queryKey: ['crm'] })
    },
    onError: (e) => setError(errorMessage(e)),
  })
  if (isSeller) {
    return (
      <Alert variant="warning">
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <AlertTitle>Aguardando aprovação do desconto</AlertTitle>
          <AlertDescription>
            O desconto de {formatBps(p.discount_bps)} passa do limite ({formatBps(p.discount_limit_bps)}). Um gestor com permissão precisa aprovar — o vendedor não aprova o próprio desconto.
          </AlertDescription>
        </div>
      </Alert>
    )
  }
  if (!can('crm.discount_approve')) {
    return (
      <Alert variant="warning">
        <AlertDescription>Aguardando aprovação do desconto por um gestor comercial.</AlertDescription>
      </Alert>
    )
  }
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Aprovar desconto</CardTitle>
          <CardDescription>
            Desconto pedido: {formatBps(p.discount_bps)} (limite {formatBps(p.discount_limit_bps)}). Justificativa: {p.discount_justification ?? EMPTY_VALUE}
          </CardDescription>
        </div>
      </CardHeader>
      {error ? (
        <Alert variant="danger">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}
      <Field>
        <FieldLabel>Comentário (obrigatório para recusar)</FieldLabel>
        <Textarea rows={2} value={reason} onChange={(e) => setReason(e.target.value)} maxLength={1000} />
      </Field>
      <div className="flex flex-wrap gap-3">
        <Button onClick={() => decide.mutate(true)} loading={decide.isPending && decide.variables === true} disabled={decide.isPending}>
          <CheckCircle2 aria-hidden="true" />
          Aprovar desconto
        </Button>
        <Button
          variant="destructive"
          disabled={decide.isPending}
          loading={decide.isPending && decide.variables === false}
          onClick={() => {
            if (!reason.trim()) {
              setError('Informe o motivo da recusa.')
              return
            }
            decide.mutate(false)
          }}
        >
          <XCircle aria-hidden="true" />
          Recusar
        </Button>
      </div>
    </Card>
  )
}

function SendCard({ p, defaultEmail }: { p: Proposal; defaultEmail: string }) {
  const qc = useQueryClient()
  const [email, setEmail] = React.useState(defaultEmail)
  const [error, setError] = React.useState<string | null>(null)
  const send = useMutation({
    mutationFn: () => crmApi.send(p.id, email.trim() || undefined),
    onSuccess: async (r) => {
      toast.success(r.emailed ? `Proposta enviada por e-mail. Válida até ${formatIsoDate(r.valid_until)}.` : `Proposta marcada como enviada (sem e-mail). Válida até ${formatIsoDate(r.valid_until)}.`)
      await qc.invalidateQueries({ queryKey: ['crm'] })
    },
    onError: (e) => setError(isApiError(e) && e.fieldErrors.email_to ? e.fieldErrors.email_to : errorMessage(e)),
  })
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Enviar ao cliente</CardTitle>
          <CardDescription>Gera o PDF definitivo e começa a contar a validade de {p.validity_days} dias.</CardDescription>
        </div>
      </CardHeader>
      <Field invalid={!!error}>
        <FieldLabel>E-mail do cliente</FieldLabel>
        <Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} className="max-w-md" />
        <FieldDescription>Em branco = e-mail do lead (se houver). O PDF vai anexado.</FieldDescription>
        <FieldError>{error}</FieldError>
      </Field>
      <Button onClick={() => send.mutate()} loading={send.isPending} loadingText="Enviando…" className="self-start">
        <Send aria-hidden="true" />
        Enviar proposta
      </Button>
    </Card>
  )
}

function ClientDecision({ p }: { p: Proposal }) {
  const qc = useQueryClient()
  const [note, setNote] = React.useState('')
  const decide = useMutation({
    mutationFn: (accepted: boolean) => crmApi.clientDecision(p.id, accepted, note.trim() || undefined),
    onSuccess: async (_r, accepted) => {
      toast.success(accepted ? 'Aceite do cliente registrado.' : 'Recusa do cliente registrada.')
      await qc.invalidateQueries({ queryKey: ['crm'] })
    },
    onError: (e) => toast.error(errorMessage(e)),
  })
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Resposta do cliente</CardTitle>
          <CardDescription>Enviada em {formatDateTime(p.sent_at)} · válida até {formatIsoDate(p.valid_until)}.</CardDescription>
        </div>
      </CardHeader>
      <Field>
        <FieldLabel>Observação</FieldLabel>
        <Textarea rows={2} value={note} onChange={(e) => setNote(e.target.value)} maxLength={1000} />
      </Field>
      <div className="flex flex-wrap gap-3">
        <Button onClick={() => decide.mutate(true)} disabled={decide.isPending} loading={decide.isPending && decide.variables === true}>
          Cliente aceitou
        </Button>
        <Button variant="secondary" onClick={() => decide.mutate(false)} disabled={decide.isPending} loading={decide.isPending && decide.variables === false}>
          Cliente recusou
        </Button>
      </div>
    </Card>
  )
}

export default function ProposalDetailPage() {
  const { id = '' } = useParams()
  const qc = useQueryClient()
  const can = useCan()
  const meId = useAuthStore((s) => s.me?.user.id)
  const dl = useDownload()
  const [editing, setEditing] = React.useState(false)
  const [converting, setConverting] = React.useState(false)
  const [cancelling, setCancelling] = React.useState(false)
  const q = useQuery({ queryKey: qk.detail('crm', `proposal:${id}`), queryFn: () => crmApi.proposal(id), enabled: !!id })
  const lead = useQuery({ queryKey: qk.detail('crm', `lead:${q.data?.lead_id ?? ''}`), queryFn: () => crmApi.lead(q.data?.lead_id as string), enabled: !!q.data?.lead_id })
  const submit = useMutation({
    mutationFn: () => crmApi.submit(id),
    onSuccess: async (p) => {
      toast.success(p.status === 'AGUARDANDO_APROVACAO' ? 'Desconto acima do limite: enviado para aprovação de um gestor.' : 'Proposta aprovada e pronta para envio.')
      await qc.invalidateQueries({ queryKey: ['crm'] })
    },
    onError: (e) => toast.error(errorMessage(e)),
  })

  if (q.isLoading) return <PageSkeleton label="Carregando proposta…" />
  if (q.error || !q.data) {
    if (isApiError(q.error) && q.error.status === 403) return <NoPermission permission="crm.view" />
    return (
      <>
        <PageHeader title="Proposta" breadcrumbs={[{ label: 'Propostas', to: '/crm/propostas' }, { label: 'Proposta' }]} />
        <ErrorState error={q.error} onRetry={() => void q.refetch()} />
      </>
    )
  }
  const p = q.data
  const manage = can('crm.manage')
  const isSeller = p.seller_id === meId

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title={`Proposta ${p.number}`}
        description={p.title}
        breadcrumbs={[{ label: 'Propostas', to: '/crm/propostas' }, { label: p.number }]}
        actions={
          <Button
            variant="secondary"
            loading={dl.isPending('pdf')}
            loadingText="Baixando…"
            onClick={() => void dl.run('pdf', `/crm/proposals/${p.id}/pdf`, { fallbackFilename: `proposta-${p.number}.pdf` })}
          >
            <Download aria-hidden="true" />
            PDF{p.status === 'RASCUNHO' ? ' (rascunho)' : ''}
          </Button>
        }
      />
      <div className="flex flex-wrap items-center gap-2">
        <StatusPill map={PROPOSAL_STATUS} value={p.status} />
        {p.order_id ? <span className="text-sm text-text-muted">Pedido {p.order_id.slice(0, 8)}…</span> : null}
      </div>

      {editing && p.status === 'RASCUNHO' ? (
        <Card>
          <CardHeader>
            <CardTitle>Editar rascunho</CardTitle>
            <Button variant="ghost" onClick={() => setEditing(false)}>
              Cancelar edição
            </Button>
          </CardHeader>
          <ProposalForm proposal={p} onSaved={() => setEditing(false)} />
        </Card>
      ) : (
        <>
          <Card>
            <DetailList
              columns={3}
              items={[
                { label: 'Cliente', value: p.org_id ? (p.org_name ?? `Empresa ${p.org_id.slice(0, 8)}`) : EMPTY_VALUE },
                { label: 'Lead', value: lead.data ? `${lead.data.name}${lead.data.company ? ` — ${lead.data.company}` : ''}` : p.lead_id ? '…' : EMPTY_VALUE },
                { label: 'Vendedor', value: personLabel(p.seller_name, p.seller_id, meId) },
                { label: 'Subtotal', value: formatCents(p.subtotal_cents), numeric: true },
                { label: 'Desconto', value: `${formatBps(p.discount_bps)} (${formatCents(p.discount_cents)})`, numeric: true },
                { label: 'Total', value: <strong>{formatCents(p.total_cents)}</strong>, numeric: true },
                { label: 'Validade', value: p.sent_at ? `até ${formatIsoDate(p.valid_until)}` : `${p.validity_days} dias após o envio` },
                { label: 'Enviada em', value: formatDateTime(p.sent_at) },
                { label: 'Aceita em', value: formatDateTime(p.accepted_at) },
                ...(p.discount_approved_by ? [{ label: 'Desconto aprovado por', value: `${personLabel(p.discount_approved_by_name, p.discount_approved_by, meId)} · ${formatDateTime(p.discount_approved_at)}` }] : []),
                ...(p.discount_decision_reason ? [{ label: 'Comentário do aprovador', value: p.discount_decision_reason, wide: true }] : []),
                ...(p.notes ? [{ label: 'Observações', value: p.notes, wide: true }] : []),
                ...(p.pdf_sha256 ? [{ label: 'SHA-256 do PDF enviado', value: <span className="break-all font-mono text-xs">{p.pdf_sha256}</span>, wide: true }] : []),
              ]}
            />
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Itens</CardTitle>
            </CardHeader>
            <Table>
              <caption className="sr-only">Itens da proposta</caption>
              <TableHeader>
                <TableRow className="h-11 hover:bg-transparent">
                  <TableHead scope="col">Item</TableHead>
                  <TableHead scope="col" numeric>
                    Qtd.
                  </TableHead>
                  <TableHead scope="col" numeric>
                    Unitário
                  </TableHead>
                  <TableHead scope="col" numeric>
                    Total
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {(p.items ?? []).map((i) => (
                  <TableRow key={i.id ?? i.description}>
                    <TableCell>
                      <div className="flex flex-col">
                        <span>{i.description}</span>
                        {i.package_code ? <span className="font-mono text-xs text-text-muted">{i.package_code}</span> : null}
                      </div>
                    </TableCell>
                    <TableCell numeric>{formatNumber(i.quantity)}</TableCell>
                    <TableCell numeric>{formatCents(i.unit_price_cents)}</TableCell>
                    <TableCell numeric>{formatCents(i.line_total_cents ?? null)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
              <TableFooter>
                <TableRow>
                  <TableCell colSpan={3} className="font-semibold">
                    Total com desconto
                  </TableCell>
                  <TableCell numeric className="font-semibold">
                    {formatCents(p.total_cents)}
                  </TableCell>
                </TableRow>
              </TableFooter>
            </Table>
          </Card>
        </>
      )}

      {manage && p.status === 'RASCUNHO' && !editing ? (
        <Card>
          <CardHeader>
            <div className="flex flex-col gap-1">
              <CardTitle>Rascunho</CardTitle>
              <CardDescription>Revise e conclua. Se o desconto passar do limite, a proposta vai para aprovação.</CardDescription>
            </div>
          </CardHeader>
          <div className="flex flex-wrap gap-3">
            <Button variant="secondary" onClick={() => setEditing(true)}>
              <Pencil aria-hidden="true" />
              Editar
            </Button>
            <Button onClick={() => submit.mutate()} loading={submit.isPending} loadingText="Concluindo…">
              <CheckCircle2 aria-hidden="true" />
              Concluir proposta
            </Button>
          </div>
        </Card>
      ) : null}
      {p.status === 'AGUARDANDO_APROVACAO' ? <DiscountDecision p={p} isSeller={isSeller} /> : null}
      {manage && p.status === 'APROVADA' ? <SendCard p={p} defaultEmail={lead.data?.email ?? ''} /> : null}
      {manage && p.status === 'ENVIADA' ? <ClientDecision p={p} /> : null}
      {p.status === 'EXPIRADA' ? (
        <Alert variant="warning">
          <AlertDescription>Proposta vencida (validade de {p.validity_days} dias). Crie uma nova proposta para o cliente.</AlertDescription>
        </Alert>
      ) : null}
      {manage && p.status === 'ACEITA' ? (
        <Card>
          <CardHeader>
            <div className="flex flex-col gap-1">
              <CardTitle>Converter em pedido</CardTitle>
              <CardDescription>Cliente aceitou em {formatDateTime(p.accepted_at)}. A conversão chama o checkout real.</CardDescription>
            </div>
          </CardHeader>
          <Button onClick={() => setConverting(true)} className="self-start">
            <ShoppingCart aria-hidden="true" />
            Converter em pedido
          </Button>
        </Card>
      ) : null}
      {manage && !TERMINAL.includes(p.status) ? (
        <Button variant="destructive" className="self-start" onClick={() => setCancelling(true)}>
          Cancelar proposta
        </Button>
      ) : null}
      {p.status === 'ENVIADA' && !manage ? (
        <p className="m-0 flex items-center gap-2 text-sm text-text-muted">
          <Mail aria-hidden="true" className="size-4" /> Enviada ao cliente em {formatDateTime(p.sent_at)}.
        </p>
      ) : null}

      <ConvertDialog proposal={p} open={converting} onOpenChange={setConverting} />
      <ReasonConfirmDialog
        open={cancelling}
        onOpenChange={setCancelling}
        title={`Cancelar a proposta ${p.number}?`}
        consequences={['A proposta é encerrada e não pode ser enviada nem convertida.']}
        minReason={3}
        requireText={p.number}
        confirmLabel="Cancelar proposta"
        onConfirm={async (reason) => {
          await crmApi.cancel(p.id, reason)
          toast.success('Proposta cancelada.')
          await qc.invalidateQueries({ queryKey: ['crm'] })
        }}
      />
    </div>
  )
}
