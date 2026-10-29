/**
 * Detalhe da empresa (RD): dados e edição, KYC simples, vínculos agência → clientes,
 * exceção de aprovação de campanhas (cliente confiável — W-CAMP) e situação
 * (inativar/reativar com motivo auditado).
 */
import * as React from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Link2Off, Plus, ShieldCheck, UserPlus } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { EmptyState, ErrorState, NoPermission, PageSkeleton } from '@/components/patterns/states'
import { useCan } from '@/hooks/usePermissions'
import { http } from '@/lib/http'
import { qk } from '@/lib/queryKeys'
import { errorMessage, isApiError } from '@/lib/errors'
import { formatDateTime } from '@/lib/format'
import { BUSINESS_ROLE_LABELS, KYC, lookup } from '@/components/admin/labels'
import { KeyValueList, MobileMeta, RealtimeStale, ReasonDialog, ToneBadge } from '@/components/admin/ui'
import { OrgPicker, type PickedEntity } from '@/components/admin/pickers'
import { useUserDirectory } from '@/components/admin/directory'
import { OrganizationForm } from './OrganizationForm'
import type { AgencyLink, ApprovalPolicies, Organization } from '../types'

export default function OrganizationDetailPage() {
  const { id = '' } = useParams()
  const can = useCan()
  const qc = useQueryClient()
  const q = useQuery({
    queryKey: qk.detail('organizations', id),
    queryFn: () => http.get<Organization>(`/organizations/${id}`),
    enabled: !!id,
  })
  const [statusDialog, setStatusDialog] = React.useState(false)

  if (q.isLoading) return <PageSkeleton />
  if (q.error || !q.data) {
    if (isApiError(q.error) && q.error.status === 403) return <NoPermission permission="organizations.view" />
    return (
      <>
        <PageHeader title="Empresa" breadcrumbs={[{ label: 'Empresas', to: '/empresas' }, { label: 'Empresa' }]} />
        <ErrorState error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />
      </>
    )
  }
  const org = q.data
  const title = org.trade_name || org.name
  const manage = can('organizations.manage')
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ['organizations'] })
  }

  const headerMeta = (
    <span className="inline-flex flex-wrap items-center gap-2">
      {org.document_formatted} · {org.person_type}
      <ToneBadge value={org.is_active ? { label: 'Ativa', tone: 'success' } : { label: 'Inativa', tone: 'neutral' }} />
    </span>
  )

  return (
    <>
      <PageHeader
        title={title}
        description={headerMeta}
        breadcrumbs={[{ label: 'Empresas', to: '/empresas' }, { label: title }]}
        actions={
          manage ? (
            <Button variant={org.is_active ? 'destructive' : 'secondary'} onClick={() => setStatusDialog(true)}>
              {org.is_active ? 'Inativar empresa' : 'Reativar empresa'}
            </Button>
          ) : undefined
        }
      />
      <div className="flex flex-col gap-4">
        <MobileMeta>{headerMeta}</MobileMeta>
        <RealtimeStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />
        {!org.is_active ? (
          <Alert variant="warning">
            <div className="flex min-w-0 flex-1 flex-col gap-1">
              <AlertTitle>Empresa inativa</AlertTitle>
              <AlertDescription>
                Inativada em {formatDateTime(org.deactivated_at)}. Motivo: {org.deactivation_reason ?? '—'}
              </AlertDescription>
            </div>
          </Alert>
        ) : null}
        <Tabs defaultValue="dados">
          <TabsList aria-label="Seções da empresa">
            <TabsTrigger value="dados">Dados</TabsTrigger>
            <TabsTrigger value="kyc">KYC</TabsTrigger>
            {org.business_roles.includes('AGENCY') ? <TabsTrigger value="clientes">Clientes da agência</TabsTrigger> : null}
            {org.business_roles.includes('ADVERTISER') && can('campaigns.approve') ? (
              <TabsTrigger value="aprovacao">Aprovação de campanhas</TabsTrigger>
            ) : null}
          </TabsList>
          <TabsContent value="dados">
            <Card>
              <CardHeader>
                <div className="flex flex-col gap-1">
                  <CardTitle>{manage ? 'Editar dados' : 'Dados cadastrais'}</CardTitle>
                  <CardDescription>
                    Papéis: {org.business_roles.map((r) => BUSINESS_ROLE_LABELS[r as keyof typeof BUSINESS_ROLE_LABELS] ?? r).join(', ') || '—'} · Atualizado em{' '}
                    {formatDateTime(org.updated_at)}
                  </CardDescription>
                </div>
              </CardHeader>
              {manage ? (
                <OrganizationForm
                  initial={org}
                  submitLabel="Salvar alterações"
                  onSubmit={async (payload) => {
                    const updated = await http.patch<Organization>(`/organizations/${org.id}`, payload)
                    qc.setQueryData(qk.detail('organizations', org.id), (old: Organization | undefined) => ({ ...old, ...updated }))
                    refresh()
                    toast.success('Dados da empresa salvos.')
                  }}
                />
              ) : (
                <KeyValueList
                  items={[
                    { label: 'Razão social / nome', value: org.name },
                    { label: 'Nome fantasia', value: org.trade_name },
                    { label: 'Responsável', value: org.responsible_name },
                    { label: 'E-mail', value: org.email },
                    { label: 'Telefone', value: org.phone },
                    {
                      label: 'Endereço',
                      value: [org.address, org.number, org.complement, org.neighborhood, org.city && `${org.city}/${org.state}`, org.postal_code]
                        .filter(Boolean)
                        .join(', '),
                      wide: true,
                    },
                  ]}
                />
              )}
            </Card>
          </TabsContent>
          <TabsContent value="kyc">
            <KycCard org={org} onChanged={refresh} />
          </TabsContent>
          {org.business_roles.includes('AGENCY') ? (
            <TabsContent value="clientes">
              <AgencyClientsCard org={org} />
            </TabsContent>
          ) : null}
          {org.business_roles.includes('ADVERTISER') && can('campaigns.approve') ? (
            <TabsContent value="aprovacao">
              <ApprovalPolicyCard org={org} />
            </TabsContent>
          ) : null}
        </Tabs>
      </div>
      <ReasonDialog
        open={statusDialog}
        onOpenChange={setStatusDialog}
        title={org.is_active ? `Inativar a empresa ${title}?` : `Reativar a empresa ${title}?`}
        description="A justificativa é registrada na auditoria."
        consequences={
          org.is_active
            ? [
                'Os estabelecimentos desta empresa também serão inativados.',
                'Telas desses estabelecimentos deixam de ser vendidas no inventário.',
              ]
            : ['A empresa volta a ficar disponível. Os estabelecimentos precisam ser reativados individualmente.']
        }
        requireText={org.is_active ? title : undefined}
        confirmLabel={org.is_active ? 'Inativar empresa' : 'Reativar empresa'}
        destructive={org.is_active}
        onConfirm={async (reason) => {
          const path = org.is_active ? 'deactivate' : 'activate'
          const res = await http.post<Organization & { establishments_deactivated?: string[] }>(`/organizations/${org.id}/${path}`, { reason })
          refresh()
          toast.success(
            org.is_active
              ? `Empresa inativada${res.establishments_deactivated?.length ? ` (${res.establishments_deactivated.length} estabelecimento(s) inativado(s))` : ''}.`
              : 'Empresa reativada.',
          )
        }}
      />
    </>
  )
}

function KycCard({ org, onChanged }: { org: Organization; onChanged: () => void }) {
  const can = useCan()
  const [status, setStatus] = React.useState<string>(org.kyc_status)
  const [notes, setNotes] = React.useState(org.kyc_notes ?? '')
  const m = useMutation({
    mutationFn: () => http.post<Organization>(`/organizations/${org.id}/kyc`, { status, notes: notes.trim() || null }),
    onSuccess: () => {
      toast.success('Revisão de KYC registrada.')
      onChanged()
    },
  })
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Verificação cadastral (KYC)</CardTitle>
          <CardDescription>Revisão simples e auditada da documentação da empresa. Somente a equipe RD com escopo global revisa.</CardDescription>
        </div>
        <ToneBadge value={lookup(KYC, org.kyc_status)} />
      </CardHeader>
      <KeyValueList
        items={[
          { label: 'Última revisão', value: formatDateTime(org.kyc_reviewed_at) },
          { label: 'Observações', value: org.kyc_notes },
        ]}
      />
      {can('organizations.manage') ? (
        <form
          noValidate
          className="flex flex-col gap-5"
          onSubmit={(e) => {
            e.preventDefault()
            m.mutate()
          }}
        >
          <FieldGroup>
            <Field>
              <FieldLabel id="kyc-status">Resultado</FieldLabel>
              <ToggleGroup type="single" aria-labelledby="kyc-status" value={status} onValueChange={(v) => v && setStatus(v)} className="w-fit">
                <ToggleGroupItem value="PENDING">Pendente</ToggleGroupItem>
                <ToggleGroupItem value="APPROVED">Aprovado</ToggleGroupItem>
                <ToggleGroupItem value="REJECTED">Reprovado</ToggleGroupItem>
              </ToggleGroup>
            </Field>
            <Field>
              <FieldLabel>Observações</FieldLabel>
              <Textarea value={notes} maxLength={2000} onChange={(e) => setNotes(e.target.value)} />
              <FieldDescription>Ex.: documentos conferidos, pendências encontradas.</FieldDescription>
            </Field>
          </FieldGroup>
          {m.error ? (
            <Alert variant="danger">
              <AlertDescription className="text-text">{errorMessage(m.error)}</AlertDescription>
            </Alert>
          ) : null}
          <div className="flex justify-end">
            <Button type="submit" loading={m.isPending}>
              <ShieldCheck aria-hidden="true" />
              Registrar revisão
            </Button>
          </div>
        </form>
      ) : null}
    </Card>
  )
}

function AgencyClientsCard({ org }: { org: Organization }) {
  const can = useCan()
  const qc = useQueryClient()
  const [includeEnded, setIncludeEnded] = React.useState(false)
  const [adding, setAdding] = React.useState(false)
  const [ending, setEnding] = React.useState<AgencyLink | null>(null)
  const [granting, setGranting] = React.useState<AgencyLink | null>(null)
  const q = useQuery({
    queryKey: qk.sub('organizations', 'detail', org.id, 'clients', { includeEnded }),
    queryFn: () =>
      http.get<{ items: AgencyLink[] }>(`/organizations/${org.id}/clients`, { query: { include_ended: includeEnded } }).then((r) => r.items),
  })
  const invalidate = () => void qc.invalidateQueries({ queryKey: ['organizations', 'detail', org.id, 'clients'] })
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Clientes da agência</CardTitle>
          <CardDescription>
            Anunciantes geridos por esta agência. Usuários da agência recebem acesso a cada cliente (perfil Agência no escopo da empresa cliente).
          </CardDescription>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="secondary" onClick={() => setIncludeEnded((v) => !v)} aria-pressed={includeEnded}>
            {includeEnded ? 'Ocultar encerrados' : 'Mostrar encerrados'}
          </Button>
          {can('organizations.manage') ? (
            <Button onClick={() => setAdding(true)}>
              <Plus aria-hidden="true" />
              Vincular cliente
            </Button>
          ) : null}
        </div>
      </CardHeader>
      {q.isLoading ? (
        <PageSkeleton label="Carregando clientes…" />
      ) : q.error ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} size="compact" />
      ) : !q.data?.length ? (
        <EmptyState size="compact" headingLevel="h3" title="Nenhum cliente vinculado." description="Vincule anunciantes que esta agência administra." />
      ) : (
        <Table>
          <caption className="sr-only">Clientes da agência</caption>
          <TableHeader>
            <TableRow>
              <TableHead scope="col">Cliente</TableHead>
              <TableHead scope="col">Situação</TableHead>
              <TableHead scope="col">Desde</TableHead>
              <TableHead scope="col" className="text-right">
                Ações
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {q.data.map((l) => (
              <TableRow key={l.id}>
                <TableCell>
                  <Link className="font-semibold text-link underline-offset-4 hover:underline" to={`/empresas/${l.client_org_id}`}>
                    {l.client_name ?? l.client_org_id}
                  </Link>
                </TableCell>
                <TableCell>
                  {l.status === 'ACTIVE' ? (
                    <Badge tone="success">Ativo</Badge>
                  ) : (
                    <span className="text-sm text-text-muted">
                      Encerrado em {formatDateTime(l.ended_at)} — {l.end_reason}
                    </span>
                  )}
                </TableCell>
                <TableCell className="tabular-nums">{formatDateTime(l.created_at)}</TableCell>
                <TableCell className="text-right">
                  {l.status === 'ACTIVE' ? (
                    <div className="flex justify-end gap-1">
                      {can('users.manage') ? (
                        <Button variant="ghost" onClick={() => setGranting(l)}>
                          <UserPlus aria-hidden="true" />
                          Dar acesso
                        </Button>
                      ) : null}
                      {can('organizations.manage') ? (
                        <Button variant="ghost" onClick={() => setEnding(l)}>
                          <Link2Off aria-hidden="true" />
                          Encerrar
                        </Button>
                      ) : null}
                    </div>
                  ) : null}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      <LinkClientDialog open={adding} onOpenChange={setAdding} agency={org} onDone={invalidate} />
      <GrantDialog link={granting} onOpenChange={(o) => !o && setGranting(null)} agency={org} />
      <ReasonDialog
        open={!!ending}
        onOpenChange={(o) => !o && setEnding(null)}
        title={`Encerrar o vínculo com ${ending?.client_name ?? 'o cliente'}?`}
        consequences={['Os acessos de usuários da agência a este cliente são revogados.', 'O histórico do vínculo é mantido.']}
        confirmLabel="Encerrar vínculo"
        destructive
        onConfirm={async (reason) => {
          if (!ending) return
          const r = await http.post<{ assignments_revoked: number }>(`/organizations/${org.id}/clients/${ending.id}/end`, { reason })
          invalidate()
          toast.success(`Vínculo encerrado (${r.assignments_revoked ?? 0} acesso(s) revogado(s)).`)
        }}
      />
    </Card>
  )
}

function LinkClientDialog({ open, onOpenChange, agency, onDone }: { open: boolean; onOpenChange: (o: boolean) => void; agency: Organization; onDone: () => void }) {
  const [client, setClient] = React.useState<PickedEntity | null>(null)
  const m = useMutation({
    mutationFn: () => http.post<AgencyLink>(`/organizations/${agency.id}/clients`, { client_org_id: client?.id }),
    onSuccess: (l) => {
      toast.success(`${l.client_name ?? 'Cliente'} vinculado à agência.`)
      onDone()
      onOpenChange(false)
    },
  })
  React.useEffect(() => {
    if (!open) {
      setClient(null)
      m.reset()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>Vincular cliente à agência</DialogTitle>
          <DialogDescription>O cliente precisa ter o papel de anunciante.</DialogDescription>
        </DialogHeader>
        <OrgPicker label="Anunciante" role="ADVERTISER" value={client} onChange={setClient} required />
        {m.error ? (
          <Alert variant="danger">
            <AlertDescription className="text-text">{errorMessage(m.error)}</AlertDescription>
          </Alert>
        ) : null}
        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Cancelar
          </Button>
          <Button onClick={() => m.mutate()} loading={m.isPending} disabled={!client}>
            Vincular
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function GrantDialog({ link, onOpenChange, agency }: { link: AgencyLink | null; onOpenChange: (o: boolean) => void; agency: Organization }) {
  const directory = useUserDirectory()
  const [userId, setUserId] = React.useState('')
  const members = directory.users.filter((u) => (u.assignments ?? []).some((a) => a.scope_type === 'ORGANIZATION' && a.scope_id === agency.id))
  const m = useMutation({
    mutationFn: () => http.post(`/organizations/${agency.id}/clients/${link?.id}/grants`, { user_id: userId }),
    onSuccess: () => {
      toast.success('Acesso concedido ao usuário da agência.')
      onOpenChange(false)
    },
  })
  React.useEffect(() => {
    if (!link) {
      setUserId('')
      m.reset()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [link])
  return (
    <Dialog open={!!link} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>Dar acesso ao cliente {link?.client_name}</DialogTitle>
          <DialogDescription>O usuário escolhido recebe o perfil Agência no escopo da empresa cliente.</DialogDescription>
        </DialogHeader>
        <Field invalid={!!m.error}>
          <FieldLabel>Usuário da agência</FieldLabel>
          <Select value={userId} onValueChange={setUserId}>
            <SelectTrigger>
              <SelectValue placeholder={directory.isLoading ? 'Carregando…' : members.length ? 'Selecione' : 'Nenhum usuário da agência encontrado'} />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                {members.map((u) => (
                  <SelectItem key={u.id} value={u.id}>
                    {u.name}
                    {u.email ? ` · ${u.email}` : ''}
                  </SelectItem>
                ))}
              </SelectGroup>
            </SelectContent>
          </Select>
          <FieldDescription>Convide usuários para a agência em Usuários → Convidar (escopo Empresa = esta agência).</FieldDescription>
          <FieldError>{m.error ? errorMessage(m.error) : undefined}</FieldError>
        </Field>
        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Cancelar
          </Button>
          <Button onClick={() => m.mutate()} loading={m.isPending} disabled={!userId}>
            Conceder acesso
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function ApprovalPolicyCard({ org }: { org: Organization }) {
  const qc = useQueryClient()
  const q = useQuery({
    queryKey: ['campaigns', 'approval-policies'],
    queryFn: () => http.get<ApprovalPolicies>('/campaigns/approval-policies'),
  })
  const [mode, setMode] = React.useState<'MANUAL' | 'AUTO'>('AUTO')
  const [editing, setEditing] = React.useState(false)
  const [removing, setRemoving] = React.useState(false)
  const current = q.data?.items.find((i) => i.org_id === org.id) ?? null
  const invalidate = () => void qc.invalidateQueries({ queryKey: ['campaigns', 'approval-policies'] })
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Aprovação de campanhas deste cliente</CardTitle>
          <CardDescription>
            Exceção por cliente confiável: sobrepõe o modo global de aprovação após o pagamento. Alterações são auditadas.
          </CardDescription>
        </div>
      </CardHeader>
      {q.isLoading ? (
        <PageSkeleton label="Carregando política…" />
      ) : q.error ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} size="compact" />
      ) : (
        <div className="flex flex-col gap-4">
          <KeyValueList
            items={[
              { label: 'Modo global', value: q.data?.global_mode === 'AUTO' ? 'Automática' : 'Manual (RD revisa)' },
              {
                label: 'Exceção deste cliente',
                value: current ? (
                  <span className="flex flex-col gap-1">
                    <span className="font-semibold">{current.mode === 'AUTO' ? 'Aprovação automática (cliente confiável)' : 'Sempre manual'}</span>
                    <span className="text-xs text-text-muted">
                      {current.reason} · {formatDateTime(current.updated_at)}
                    </span>
                  </span>
                ) : (
                  'Sem exceção — segue o modo global'
                ),
              },
            ]}
          />
          <div className="flex flex-wrap gap-2">
            <Button
              onClick={() => {
                setMode(current?.mode === 'AUTO' ? 'MANUAL' : 'AUTO')
                setEditing(true)
              }}
            >
              {current ? 'Alterar exceção' : 'Definir exceção'}
            </Button>
            {current ? (
              <Button variant="destructive" onClick={() => setRemoving(true)}>
                Remover exceção
              </Button>
            ) : null}
          </div>
        </div>
      )}
      <ReasonDialog
        open={editing}
        onOpenChange={setEditing}
        title="Exceção de aprovação"
        description="Defina como as campanhas pagas deste cliente são aprovadas."
        confirmLabel="Salvar exceção"
        onConfirm={async (reason) => {
          await http.put(`/campaigns/approval-policies/${org.id}`, { mode, reason })
          invalidate()
          toast.success('Exceção de aprovação salva.')
        }}
      >
        <Field>
          <FieldLabel id="policy-mode">Modo</FieldLabel>
          <ToggleGroup type="single" aria-labelledby="policy-mode" value={mode} onValueChange={(v) => v && setMode(v as 'MANUAL' | 'AUTO')} className="w-fit">
            <ToggleGroupItem value="AUTO">Automática (confiável)</ToggleGroupItem>
            <ToggleGroupItem value="MANUAL">Sempre manual</ToggleGroupItem>
          </ToggleGroup>
        </Field>
      </ReasonDialog>
      <ReasonDialog
        open={removing}
        onOpenChange={setRemoving}
        title="Remover a exceção deste cliente?"
        consequences={['As próximas campanhas deste cliente seguem o modo global de aprovação.']}
        minLength={0}
        reasonLabel="Observação"
        confirmLabel="Remover exceção"
        destructive
        onConfirm={async () => {
          await http.delete(`/campaigns/approval-policies/${org.id}`)
          invalidate()
          toast.success('Exceção removida.')
        }}
      />
    </Card>
  )
}
