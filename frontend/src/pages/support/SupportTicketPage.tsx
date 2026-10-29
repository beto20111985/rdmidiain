/**
 * Detalhe do chamado — GET /support/tickets/{id}: dados, SLA, histórico e conversa.
 * Comentários INTERNOS só existem para a equipe RD: o servidor não os envia ao cliente e a
 * tela também os descarta fora da visão da equipe. Estabelecimento nunca vê o anunciante
 * (o servidor omite; a tela só mostra o código do anúncio e a situação).
 */
import * as React from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Lock, Send, UserCheck } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Textarea } from '@/components/ui/textarea'
import { Checkbox } from '@/components/ui/checkbox'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Field, FieldLabel } from '@/components/ui/field'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ErrorState, NoPermission, PageSkeleton } from '@/components/patterns/states'
import { useAuthStore } from '@/stores/authStore'
import { useActiveRouteContext } from '@/stores/contextStore'
import { http } from '@/lib/http'
import { qk } from '@/lib/queryKeys'
import { errorMessage, isApiError } from '@/lib/errors'
import { formatDateTime } from '@/lib/format'
import { cn } from '@/lib/utils'
import {
  TICKET_CATEGORY,
  TICKET_CLIENT_TRANSITIONS,
  TICKET_PRIORITY,
  TICKET_STATUS,
  TICKET_TRANSITIONS,
  labelOf,
  lookup,
} from '@/components/admin/labels'
import { KeyValueList, MobileMeta, RealtimeStale, ReasonDialog, ToneBadge } from '@/components/admin/ui'
import { useAssignableUsers, useUserDirectory } from '@/components/admin/directory'
import { useScopeLabels } from '@/components/admin/scope-labels'
import { SUPPORT_BASE } from '@/components/admin/support-links'
import { isStaffView, type TicketDetail, type TicketMessage } from './types'

/** Visão do cliente: segue a situação (não só a atribuição) para não contradizer o histórico. */
function clientAttendance(t: Pick<TicketDetail, 'status' | 'assigned'>): string | null {
  if (t.status === 'AGUARDANDO_CLIENTE') return 'Aguardando sua resposta'
  if (t.status === 'RESOLVIDO' || t.status === 'FECHADO' || t.status === 'CANCELADO') return null
  if (t.status === 'EM_ATENDIMENTO' || t.assigned) return 'Em atendimento pela equipe RD'
  return 'Aguardando a equipe RD'
}

const STATUS_ACTION: Record<string, string> = {
  EM_ATENDIMENTO: 'Colocar em atendimento',
  AGUARDANDO_CLIENTE: 'Aguardar cliente',
  RESOLVIDO: 'Marcar como resolvido',
  FECHADO: 'Fechar chamado',
  CANCELADO: 'Cancelar chamado',
}

export default function SupportTicketPage() {
  const { id = '' } = useParams()
  const ctx = useActiveRouteContext() ?? 'rd'
  const base = SUPPORT_BASE[ctx]
  const me = useAuthStore((s) => s.me)
  const qc = useQueryClient()
  const q = useQuery({ queryKey: qk.detail('support', id), queryFn: () => http.get<TicketDetail>(`/support/tickets/${id}`), enabled: !!id })
  const t = q.data
  const staff = t ? isStaffView(t) : false
  const directory = useUserDirectory()
  // Equipe RD: quem abriu (empresa/unidade solicitante) — a API devolve só os ids.
  const requesterScopes = staff && t
    ? [
        ...(t.org_id ? [{ scope_type: 'ORGANIZATION', scope_id: t.org_id }] : []),
        ...(t.establishment_id ? [{ scope_type: 'ESTABLISHMENT', scope_id: t.establishment_id }] : []),
      ]
    : []
  const scopeLabel = useScopeLabels(requesterScopes)
  const assignees = useAssignableUsers('support', 'support.manage', '/support/assignees', staff, { ticket_id: id })
  // statusTo continua definido após fechar: o título/rótulo não "piscam" durante a animação de saída.
  const [statusTo, setStatusTo] = React.useState<string | null>(null)
  const [statusOpen, setStatusOpen] = React.useState(false)
  const [body, setBody] = React.useState('')
  const [internal, setInternal] = React.useState(false)

  const invalidate = () => void qc.invalidateQueries({ queryKey: ['support'] })
  const send = useMutation({
    mutationFn: () => http.post(`/support/tickets/${id}/messages`, { body: body.trim(), is_internal: staff && internal }),
    onSuccess: () => {
      setBody('')
      setInternal(false)
      invalidate()
      toast.success(staff && internal ? 'Comentário interno registrado.' : 'Mensagem enviada.')
    },
  })
  const assign = useMutation({
    mutationFn: (userId: string | null) => http.post(`/support/tickets/${id}/assign`, { user_id: userId }),
    onSuccess: () => {
      invalidate()
      toast.success('Responsável atualizado.')
    },
    onError: (e) => toast.error(errorMessage(e)),
  })
  const priority = useMutation({
    mutationFn: (p: string) => http.post(`/support/tickets/${id}/priority`, { priority: p }),
    onSuccess: () => {
      invalidate()
      toast.success('Prioridade atualizada (prazo recalculado).')
    },
    onError: (e) => toast.error(errorMessage(e)),
  })

  if (q.isLoading) return <PageSkeleton />
  if (q.error || !t) {
    if (isApiError(q.error) && q.error.status === 403) return <NoPermission permission="support.view" />
    return (
      <>
        <PageHeader title="Chamado" breadcrumbs={[{ label: 'Suporte', to: base }, { label: 'Chamado' }]} />
        <ErrorState error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />
      </>
    )
  }

  const closed = t.status === 'FECHADO' || t.status === 'CANCELADO'
  const transitions = staff
    ? TICKET_TRANSITIONS[t.status] ?? []
    : TICKET_CLIENT_TRANSITIONS.filter(([from]) => from === t.status).map(([, to]) => to)
  // Defesa extra: fora da visão da equipe, nada interno é exibido.
  const messages = staff ? t.messages : t.messages.filter((m) => !m.is_internal)
  // Nome do autor vem do servidor (`author_name`; o cliente recebe "Equipe RD" e sem id).
  const authorOf = (m: TicketMessage) => {
    if (m.author_id && m.author_id === me?.user.id) return 'Você'
    if (m.author_name) return staff && m.author_is_staff ? `${m.author_name} (Equipe RD)` : m.author_name
    if (staff && m.author_id) return directory.nameOf(m.author_id) ?? (m.author_id === t.created_by ? 'Solicitante' : 'Equipe RD')
    return m.author_is_staff ? 'Equipe RD' : 'Participante'
  }
  const assigneeName = t.assigned_to ? directory.nameOf(t.assigned_to) ?? assignees.data?.users.find((u) => u.id === t.assigned_to)?.name ?? (t.assigned_to === me?.user.id ? 'Você' : 'Técnico atribuído') : null

  const headerMeta = (
    <span className="inline-flex flex-wrap items-center gap-2">
      <ToneBadge value={lookup(TICKET_STATUS, t.status)} />
      <ToneBadge value={lookup(TICKET_PRIORITY, t.priority)} />
      {t.sla_breached ? <Badge tone="danger">Prazo vencido</Badge> : null}
    </span>
  )

  return (
    <>
      <PageHeader
        title={`${t.number} · ${t.title}`}
        description={headerMeta}
        breadcrumbs={[{ label: 'Suporte', to: base }, { label: t.number }]}
      />
      <div className="flex flex-col gap-4">
        <MobileMeta>{headerMeta}</MobileMeta>
        <RealtimeStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-12 xl:gap-6">
          <div className="flex flex-col gap-4 xl:col-span-8">
            <Card>
              <CardHeader>
                <CardTitle>Descrição</CardTitle>
              </CardHeader>
              <p className="whitespace-pre-line text-sm text-text">{t.description}</p>
            </Card>
            <Card>
              <CardHeader>
                <div className="flex flex-col gap-1">
                  <CardTitle>Conversa</CardTitle>
                  {staff ? <CardDescription>Comentários internos aparecem com cadeado e nunca são enviados ao cliente.</CardDescription> : null}
                </div>
              </CardHeader>
              {messages.length ? (
                <ol className="m-0 flex list-none flex-col gap-3 p-0" aria-label="Mensagens do chamado">
                  {messages.map((m) => (
                    <li
                      key={m.id}
                      className={cn('flex flex-col gap-1 rounded-lg border p-3', m.is_internal ? 'border-warning/60 bg-warning-soft' : 'border-border bg-surface')}
                    >
                      <span className="flex flex-wrap items-center gap-2 text-xs text-text-muted">
                        <strong className="text-sm text-text">{authorOf(m)}</strong>
                        <time dateTime={m.created_at}>{formatDateTime(m.created_at)}</time>
                        {m.is_internal ? (
                          <Badge tone="warning">
                            <Lock aria-hidden="true" />
                            Interno — só equipe RD
                          </Badge>
                        ) : null}
                      </span>
                      <p className="m-0 whitespace-pre-line text-sm text-text">{m.body}</p>
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="text-sm text-text-muted">Nenhuma mensagem ainda.</p>
              )}
              {closed ? (
                <Alert variant="neutral">
                  <AlertDescription>Chamado encerrado. Para um novo assunto, abra outro chamado.</AlertDescription>
                </Alert>
              ) : (
                <form
                  noValidate
                  className="flex flex-col gap-3 border-t border-border pt-4"
                  onSubmit={(e) => {
                    e.preventDefault()
                    if (body.trim()) send.mutate()
                  }}
                >
                  <Field>
                    <FieldLabel>{staff && internal ? 'Comentário interno' : 'Sua mensagem'}</FieldLabel>
                    <Textarea value={body} maxLength={10000} onChange={(e) => setBody(e.target.value)} />
                  </Field>
                  {staff ? (
                    <Field orientation="horizontal" className="min-h-11">
                      <Checkbox checked={internal} onCheckedChange={(c) => setInternal(c === true)} />
                      <FieldLabel className="font-normal">Comentário interno (visível só para a equipe RD)</FieldLabel>
                    </Field>
                  ) : null}
                  {send.error ? (
                    <Alert variant="danger">
                      <AlertDescription className="text-text">{errorMessage(send.error)}</AlertDescription>
                    </Alert>
                  ) : null}
                  <div className="flex justify-end">
                    <Button type="submit" loading={send.isPending} disabled={!body.trim()}>
                      <Send aria-hidden="true" />
                      {staff && internal ? 'Registrar comentário interno' : 'Enviar mensagem'}
                    </Button>
                  </div>
                </form>
              )}
            </Card>
          </div>
          <div className="flex flex-col gap-4 xl:col-span-4">
            <Card>
              <CardHeader>
                <CardTitle>Dados do chamado</CardTitle>
              </CardHeader>
              <KeyValueList
                className="md:grid-cols-1"
                items={[
                  { label: 'Assunto', value: labelOf(TICKET_CATEGORY, t.category) },
                  { label: 'Código do anúncio', value: t.creative_code ? <code className="font-mono">{t.creative_code}</code> : null },
                  ...(staff
                    ? [
                        {
                          label: 'Empresa solicitante',
                          value: t.org_id ? (
                            <Link to={`/empresas/${t.org_id}`} className="text-link underline-offset-4 hover:underline">
                              {scopeLabel({ scope_type: 'ORGANIZATION', scope_id: t.org_id }) ?? 'Ver empresa'}
                            </Link>
                          ) : null,
                        },
                        ...(t.establishment_id
                          ? [{ label: 'Unidade', value: scopeLabel({ scope_type: 'ESTABLISHMENT', scope_id: t.establishment_id }) ?? 'Unidade vinculada' }]
                          : []),
                        { label: 'Aberto por', value: t.created_by === me?.user.id ? 'Você' : directory.nameOf(t.created_by) ?? 'Solicitante' },
                        {
                          label: 'Anúncio localizado',
                          value: t.creative_code ? (t.creative_found ? 'Sim' : 'Não encontrado — confira o código') : null,
                        },
                        {
                          label: 'Anunciante',
                          value: t.advertiser_org_id ? (
                            <Link to={`/empresas/${t.advertiser_org_id}`} className="text-link underline-offset-4 hover:underline">
                              Ver empresa anunciante
                            </Link>
                          ) : null,
                        },
                        {
                          label: 'Campanha',
                          value: t.campaign_id ? (
                            <Link to={`/campanhas/${t.campaign_id}`} className="text-link underline-offset-4 hover:underline">
                              Ver campanha
                            </Link>
                          ) : null,
                        },
                        { label: 'Responsável', value: assigneeName ?? 'Sem responsável' },
                      ]
                    : [{ label: 'Atendimento', value: clientAttendance(t) }]),
                  { label: 'Prazo (SLA)', value: `${formatDateTime(t.sla_due_at)}${t.sla_breached ? ' — vencido' : ''}` },
                  { label: 'Aberto em', value: formatDateTime(t.created_at) },
                  { label: 'Primeira resposta', value: formatDateTime(t.first_response_at) },
                  { label: 'Resolvido em', value: formatDateTime(t.resolved_at) },
                  { label: 'Encerrado em', value: formatDateTime(t.closed_at) },
                ]}
              />
            </Card>
            {transitions.length ? (
              <Card>
                <CardHeader>
                  <CardTitle>Situação</CardTitle>
                </CardHeader>
                <div className="flex flex-col gap-2">
                  {transitions.map((to) => (
                    <Button key={to} variant={to === 'CANCELADO' ? 'destructive' : 'secondary'} onClick={() => { setStatusTo(to); setStatusOpen(true) }}>
                      {!staff && to === 'EM_ATENDIMENTO' ? 'Reabrir (não foi resolvido)' : STATUS_ACTION[to] ?? to}
                    </Button>
                  ))}
                </div>
              </Card>
            ) : null}
            {staff && !closed ? (
              <Card>
                <CardHeader>
                  <div className="flex flex-col gap-1">
                    <CardTitle>Atendimento</CardTitle>
                    <CardDescription>Atribuir o chamado e ajustar a prioridade (o prazo é recalculado).</CardDescription>
                  </div>
                </CardHeader>
                <div className="flex flex-col gap-4">
                  {t.assigned_to !== me?.user.id ? (
                    <Button variant="secondary" onClick={() => assign.mutate(null)} loading={assign.isPending && assign.variables === null}>
                      <UserCheck aria-hidden="true" />
                      Assumir chamado
                    </Button>
                  ) : null}
                  {assignees.data?.users.length ? (
                    <Field>
                      <FieldLabel>Atribuir a</FieldLabel>
                      <Select value={t.assigned_to ?? ''} onValueChange={(v) => assign.mutate(v)}>
                        <SelectTrigger>
                          <SelectValue placeholder="Escolha o técnico" />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectGroup>
                            {assignees.data.users.map((u) => (
                              <SelectItem key={u.id} value={u.id}>
                                {u.name}
                                {typeof u.load === 'number' ? ` · ${u.load} chamado(s) aberto(s)` : ''}
                              </SelectItem>
                            ))}
                          </SelectGroup>
                        </SelectContent>
                      </Select>
                    </Field>
                  ) : null}
                  <Field>
                    <FieldLabel>Prioridade</FieldLabel>
                    <Select value={t.priority} onValueChange={(p) => p !== t.priority && priority.mutate(p)}>
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectGroup>
                          {Object.entries(TICKET_PRIORITY).map(([k, v]) => (
                            <SelectItem key={k} value={k}>
                              {v.label}
                            </SelectItem>
                          ))}
                        </SelectGroup>
                      </SelectContent>
                    </Select>
                  </Field>
                </div>
              </Card>
            ) : null}
            <Card>
              <CardHeader>
                <CardTitle>Histórico</CardTitle>
              </CardHeader>
              {t.history.length ? (
                <ol className="m-0 flex list-none flex-col gap-2 p-0 text-sm">
                  {t.history.map((h, i) => (
                    <li key={i} className="flex flex-col">
                      <span className="text-text">
                        {h.type === 'CRIADO'
                          ? 'Chamado aberto'
                          : h.type === 'STATUS'
                            ? `Situação: ${lookup(TICKET_STATUS, h.from).label} → ${lookup(TICKET_STATUS, h.to).label}`
                            : h.type === 'PRIORIDADE'
                              ? `Prioridade: ${lookup(TICKET_PRIORITY, h.from).label} → ${lookup(TICKET_PRIORITY, h.to).label}`
                              : h.type === 'ATRIBUICAO'
                                ? 'Responsável alterado'
                                : h.type}
                      </span>
                      <time dateTime={h.at} className="text-xs text-text-muted">
                        {formatDateTime(h.at)}
                      </time>
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="text-sm text-text-muted">Sem registros.</p>
              )}
            </Card>
          </div>
        </div>
      </div>
      <ReasonDialog
        open={statusOpen}
        onOpenChange={setStatusOpen}
        title={statusTo ? `${STATUS_ACTION[statusTo] ?? statusTo}?` : ''}
        description={statusTo === 'CANCELADO' ? 'O chamado é encerrado sem solução. Para um novo assunto, abra outro chamado.' : undefined}
        reasonLabel="Observação (opcional)"
        minLength={0}
        confirmLabel={statusTo ? STATUS_ACTION[statusTo] ?? 'Confirmar' : 'Confirmar'}
        destructive={statusTo === 'CANCELADO'}
        onConfirm={async (note) => {
          if (!statusTo) return
          await http.post(`/support/tickets/${t.id}/status`, { status: statusTo, note: note || null })
          invalidate()
          toast.success('Situação do chamado atualizada.')
        }}
      />
    </>
  )
}
