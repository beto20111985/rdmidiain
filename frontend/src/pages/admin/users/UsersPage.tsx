/**
 * Usuários (RD) — GET /users (filtrado pelo escopo do ator no servidor) e convites
 * pendentes (GET/DELETE /auth/invites). Admin Master visto por outra pessoa: só nome e
 * situação (o servidor oculta o resto).
 */
import * as React from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { MailPlus, ShieldAlert, Users } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Field, FieldLabel } from '@/components/ui/field'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Skeleton } from '@/components/ui/skeleton'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { EmptyState, ErrorState } from '@/components/patterns/states'
import { ConfirmDialog } from '@/components/patterns/confirm-dialog'
import { useCan } from '@/hooks/usePermissions'
import { useTableState } from '@/hooks/useTableState'
import { http, type Paginated } from '@/lib/http'
import { qk } from '@/lib/queryKeys'
import { formatDateTime } from '@/lib/format'
import { ROLE_LABELS, labelOf } from '@/components/admin/labels'
import { RealtimeStale, ToneBadge } from '@/components/admin/ui'
import { useTableSearch } from '@/components/admin/hooks'
import { useScopeLabels } from '@/components/admin/scope-labels'
import { InviteDialog } from './InviteDialog'
import { scopeText } from './roleScope'
import type { Invite, UserRow } from '../types'

function statusOf(u: UserRow) {
  if (!u.is_active) return { label: 'Inativo', tone: 'neutral' as const }
  if (u.locked) return { label: 'Bloqueado', tone: 'danger' as const }
  return { label: 'Ativo', tone: 'success' as const }
}

export default function UsersPage() {
  const can = useCan()
  const [sp, setSp] = useSearchParams()
  const tab = sp.get('aba') === 'convites' && can('users.manage') ? 'convites' : 'usuarios'
  const [inviting, setInviting] = React.useState(false)

  const action = can('users.manage') ? (
    <Button onClick={() => setInviting(true)}>
      <MailPlus aria-hidden="true" />
      Convidar usuário
    </Button>
  ) : undefined

  return (
    <>
      <PageHeader title="Usuários" description="Usuários no seu escopo, perfis atribuídos e convites." actions={action} />
      <Tabs
        value={tab}
        onValueChange={(v) =>
          setSp(
            (prev) => {
              const n = new URLSearchParams(prev)
              if (v === 'convites') n.set('aba', 'convites')
              else n.delete('aba')
              return n
            },
            { replace: true },
          )
        }
      >
        <TabsList aria-label="Usuários e convites">
          <TabsTrigger value="usuarios">Usuários</TabsTrigger>
          {can('users.manage') ? <TabsTrigger value="convites">Convites pendentes</TabsTrigger> : null}
        </TabsList>
        <TabsContent value="usuarios">
          <UsersTable action={action} />
        </TabsContent>
        {can('users.manage') ? (
          <TabsContent value="convites">
            <InvitesTable onInvite={() => setInviting(true)} />
          </TabsContent>
        ) : null}
      </Tabs>
      <InviteDialog open={inviting} onOpenChange={setInviting} />
    </>
  )
}

function UsersTable({ action }: { action?: React.ReactNode }) {
  const table = useTableState({ defaultPageSize: 25, filterKeys: ['is_active'] })
  const [search, setSearch] = useTableSearch(table)
  const params = {
    page: table.state.page,
    page_size: table.state.pageSize,
    search: table.state.q || undefined,
    is_active: table.state.filters.is_active,
  }
  const q = useQuery({
    queryKey: qk.list('users', params),
    queryFn: () => http.get<Paginated<UserRow>>('/users', { query: params }),
    placeholderData: keepPreviousData,
  })
  const labelOfScope = useScopeLabels((q.data?.items ?? []).flatMap((u) => u.assignments ?? []))

  const columns: DataTableColumn<UserRow>[] = [
    {
      id: 'name',
      header: 'Nome',
      mobile: 'primary',
      cell: (u) => (
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="flex flex-wrap items-center gap-2">
            <Link to={`/usuarios/${u.id}`} className="truncate font-semibold text-link underline-offset-4 hover:underline">
              {u.name}
            </Link>
            {u.is_admin_master ? (
              <Badge tone="info">
                <ShieldAlert aria-hidden="true" />
                Admin Master
              </Badge>
            ) : null}
          </span>
          {u.restricted ? <span className="text-xs text-text-muted">Dados protegidos (Admin Master)</span> : null}
        </div>
      ),
    },
    { id: 'email', header: 'E-mail', mobile: 'secondary', cell: (u) => (u.restricted ? '—' : u.email) },
    {
      id: 'roles',
      header: 'Perfis e escopos',
      mobile: 'field',
      cell: (u) =>
        u.restricted ? (
          '—'
        ) : (u.assignments ?? []).length ? (
          <ul className="m-0 flex list-none flex-col gap-0.5 p-0 text-sm">
            {(u.assignments ?? []).map((a) => (
              <li key={a.id}>
                <span className="font-semibold">{labelOf(ROLE_LABELS, a.role)}</span>{' '}
                <span className="text-text-muted">· {scopeText({ ...a, scope_label: labelOfScope(a) })}</span>
              </li>
            ))}
          </ul>
        ) : (
          <span className="text-text-muted">Sem perfil</span>
        ),
    },
    {
      id: 'mfa',
      header: '2FA',
      mobile: 'field',
      cell: (u) => (u.restricted || u.mfa_enabled === undefined ? '—' : u.mfa_enabled ? 'Ativa' : 'Desativada'),
    },
    { id: 'last', header: 'Último acesso', mobile: 'field', cell: (u) => (u.restricted ? '—' : formatDateTime(u.last_login)) },
    { id: 'status', header: 'Situação', mobile: 'badge', cell: (u) => <ToneBadge value={statusOf(u)} /> },
  ]

  return (
    <div className="flex flex-col gap-4">
      <RealtimeStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />
      <Card>
        <DataTable
          caption="Usuários"
          columns={columns}
          data={q.data}
          getRowId={(u) => u.id}
          controller={table}
          isLoading={q.isLoading}
          isFetching={q.isFetching}
          error={q.error}
          onRetry={() => void q.refetch()}
          filtersActive={!!table.state.q || Object.keys(table.state.filters).length > 0}
          empty={{ icon: Users, title: 'Nenhum usuário no seu escopo.', description: 'Convide pessoas para dar acesso ao painel.', action }}
          toolbar={
            <>
              <Field className="w-full sm:max-w-xs">
                <FieldLabel>Buscar</FieldLabel>
                <Input type="search" placeholder="Nome ou e-mail" value={search} onChange={(e) => setSearch(e.target.value)} />
              </Field>
              <Field className="w-full sm:w-44">
                <FieldLabel>Situação</FieldLabel>
                <Select value={table.state.filters.is_active ?? 'all'} onValueChange={(v) => table.setFilter('is_active', v === 'all' ? null : v)}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      <SelectItem value="all">Todos</SelectItem>
                      <SelectItem value="true">Ativos</SelectItem>
                      <SelectItem value="false">Inativos</SelectItem>
                    </SelectGroup>
                  </SelectContent>
                </Select>
              </Field>
            </>
          }
        />
      </Card>
    </div>
  )
}

function InvitesTable({ onInvite }: { onInvite: () => void }) {
  const qc = useQueryClient()
  const [revoking, setRevoking] = React.useState<Invite | null>(null)
  const q = useQuery({
    queryKey: qk.sub('users', 'invites'),
    queryFn: () => http.get<{ items: Invite[] }>('/auth/invites').then((r) => r.items),
  })
  const labelOfScope = useScopeLabels(q.data ?? [])
  const revoke = useMutation({
    mutationFn: (id: string) => http.delete(`/auth/invites/${id}`),
    onSuccess: () => {
      toast.success('Convite revogado.')
      void qc.invalidateQueries({ queryKey: ['users', 'invites'] })
    },
  })
  let body: React.ReactNode
  if (q.isLoading)
    body = (
      <div role="status" className="flex flex-col gap-2">
        <span className="sr-only">Carregando convites…</span>
        {Array.from({ length: 3 }).map((_, i) => (
          <Skeleton key={i} className="h-[52px] w-full" />
        ))}
      </div>
    )
  else if (q.error) body = <ErrorState error={q.error} onRetry={() => void q.refetch()} size="compact" />
  else if (!q.data?.length)
    body = (
      <EmptyState
        size="compact"
        headingLevel="h3"
        icon={MailPlus}
        title="Nenhum convite pendente."
        description="Convites aceitos, revogados ou vencidos não aparecem aqui."
        action={<Button onClick={onInvite}>Convidar usuário</Button>}
      />
    )
  else
    body = (
      <div className="overflow-x-auto">
        <Table>
          <caption className="sr-only">Convites pendentes</caption>
          <TableHeader>
            <TableRow>
              <TableHead scope="col">E-mail</TableHead>
              <TableHead scope="col">Perfil</TableHead>
              <TableHead scope="col">Escopo</TableHead>
              <TableHead scope="col">Vence em</TableHead>
              <TableHead scope="col" className="text-right">
                Ações
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {q.data.map((i) => (
              <TableRow key={i.id}>
                <TableCell className="font-semibold">{i.email}</TableCell>
                <TableCell>{labelOf(ROLE_LABELS, i.role)}</TableCell>
                <TableCell>{scopeText({ ...i, scope_label: labelOfScope(i) })}</TableCell>
                <TableCell className="tabular-nums">{formatDateTime(i.expires_at)}</TableCell>
                <TableCell className="text-right">
                  <Button variant="ghost" onClick={() => setRevoking(i)}>
                    Revogar
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    )
  return (
    <Card>
      {body}
      <ConfirmDialog
        open={!!revoking}
        onOpenChange={(o) => !o && setRevoking(null)}
        title={`Revogar o convite de ${revoking?.email ?? ''}?`}
        description="O link enviado por e-mail deixa de funcionar."
        confirmLabel="Revogar convite"
        destructive
        onConfirm={async () => {
          if (revoking) await revoke.mutateAsync(revoking.id)
        }}
      />
    </Card>
  )
}
