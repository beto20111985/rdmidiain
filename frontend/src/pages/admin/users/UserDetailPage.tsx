/**
 * Detalhe do usuário: dados, atribuições (perfil × escopo), segurança (desativar, encerrar
 * sessões, redefinir 2FA, desbloquear — com motivo auditado) e impersonation.
 * Admin Master: o outro Admin Master vê só nome e situação; nenhuma ação é oferecida
 * (o servidor bloqueia com ADMIN_MASTER_PROTECTED).
 */
import * as React from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { KeyRound, LockOpen, LogOut, Plus, ShieldAlert, Trash2, UserCheck, UserRoundCog, UserX } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Field, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { ErrorState, NoPermission, PageSkeleton } from '@/components/patterns/states'
import { ConfirmDialog } from '@/components/patterns/confirm-dialog'
import { useCan } from '@/hooks/usePermissions'
import { useAuthStore } from '@/stores/authStore'
import { http } from '@/lib/http'
import { qk } from '@/lib/queryKeys'
import { isApiError } from '@/lib/errors'
import { formatDateTime } from '@/lib/format'
import { IMPERSONATION_BLOCKED_ROLES, ROLE_LABELS, labelOf } from '@/components/admin/labels'
import { KeyValueList, ReasonDialog, ToneBadge } from '@/components/admin/ui'
import { RoleScopeFields } from './RoleScopeFields'
import { useScopeLabels } from '@/components/admin/scope-labels'
import { EMPTY_ROLE_SCOPE, roleScopePayload, scopeText, type RoleScopeValue } from './roleScope'
import { explainUserError, type ExplainedError } from './userErrors'
import { ImpersonateDialog } from './ImpersonateDialog'
import type { UserAssignment, UserRow } from '../types'

type SecurityAction = 'sessions' | 'mfa' | 'unlock' | null

export default function UserDetailPage() {
  const { id = '' } = useParams()
  const can = useCan()
  const me = useAuthStore((s) => s.me)
  const qc = useQueryClient()
  const q = useQuery({ queryKey: qk.detail('users', id), queryFn: () => http.get<UserRow>(`/users/${id}`), enabled: !!id })
  const labelOfScope = useScopeLabels(q.data?.assignments ?? [])
  const scopeOf = (a: UserAssignment) => scopeText({ ...a, scope_label: labelOfScope(a) })
  const [impersonating, setImpersonating] = React.useState(false)
  const [security, setSecurity] = React.useState<SecurityAction>(null)
  const [toggleActive, setToggleActive] = React.useState(false)
  const [adding, setAdding] = React.useState(false)
  const [removing, setRemoving] = React.useState<UserAssignment | null>(null)

  if (q.isLoading) return <PageSkeleton />
  if (q.error || !q.data) {
    if (isApiError(q.error) && q.error.status === 403) return <NoPermission permission="users.view" />
    return (
      <>
        <PageHeader title="Usuário" breadcrumbs={[{ label: 'Usuários', to: '/usuarios' }, { label: 'Usuário' }]} />
        <ErrorState error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />
      </>
    )
  }
  const u = q.data
  const isSelf = u.id === me?.user.id
  const invalidate = () => void qc.invalidateQueries({ queryKey: ['users'] })
  const breadcrumbs = [{ label: 'Usuários', to: '/usuarios' }, { label: u.name }]

  if (u.restricted) {
    return (
      <>
        <PageHeader title={u.name} breadcrumbs={breadcrumbs} />
        <Card>
          <CardHeader>
            <CardTitle>Admin Master</CardTitle>
            <ToneBadge value={u.is_active ? { label: 'Ativo', tone: 'success' } : { label: 'Inativo', tone: 'neutral' }} />
          </CardHeader>
          <Alert variant="info">
            <ShieldAlert aria-hidden="true" />
            <div className="flex flex-col gap-1">
              <AlertTitle>Os dois Admin Master são independentes</AlertTitle>
              <AlertDescription>
                Um Admin Master não vê nem altera e-mail, telefone, senha, 2FA, sessões, perfis ou situação do outro. Por isso só o nome e a
                situação aparecem aqui, e nenhuma ação está disponível.
              </AlertDescription>
            </div>
          </Alert>
        </Card>
      </>
    )
  }

  const manage = can('users.manage') && !isSelf && !u.is_admin_master && !me?.impersonation?.active
  const privileged = (u.assignments ?? []).some((a) => IMPERSONATION_BLOCKED_ROLES.includes(a.role))
  const impersonationBlock = u.is_admin_master
    ? 'Não é permitido acessar como um Admin Master.'
    : privileged
      ? 'Não é permitido acessar como administradores (Super Admin, Admin ou Financeiro).'
      : !u.is_active
        ? 'Usuário inativo.'
        : isSelf
          ? 'Esta é a sua própria conta.'
          : me?.impersonation?.active
            ? 'Encerre o acesso atual antes de iniciar outro.'
            : null

  const status = !u.is_active ? { label: 'Inativo', tone: 'neutral' as const } : u.locked ? { label: 'Bloqueado', tone: 'danger' as const } : { label: 'Ativo', tone: 'success' as const }

  return (
    <>
      <PageHeader
        title={u.name}
        description={<span className="inline-flex flex-wrap items-center gap-2">{u.email} <ToneBadge value={status} /></span>}
        breadcrumbs={breadcrumbs}
        actions={
          can('users.impersonate') ? (
            <Button variant="secondary" onClick={() => setImpersonating(true)} disabled={!!impersonationBlock} title={impersonationBlock ?? undefined}>
              <UserRoundCog aria-hidden="true" />
              Acessar como este usuário
            </Button>
          ) : undefined
        }
      />
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-12 xl:gap-6">
        <div className="flex flex-col gap-4 xl:col-span-8">
          {u.is_admin_master && isSelf ? (
            <Alert variant="info">
              <AlertDescription>
                Esta é a sua conta de Admin Master. Seus dados de segurança são alterados somente por você, em Minha conta → Segurança.
              </AlertDescription>
            </Alert>
          ) : null}
          {can('users.impersonate') && impersonationBlock ? (
            <Alert variant="neutral">
              <AlertDescription>Acesso como este usuário indisponível: {impersonationBlock}</AlertDescription>
            </Alert>
          ) : null}
          <Card>
            <CardHeader>
              <CardTitle>Dados do usuário</CardTitle>
            </CardHeader>
            <KeyValueList
              items={[
                { label: 'E-mail', value: u.email },
                { label: 'Telefone', value: u.phone },
                { label: 'Verificação em duas etapas', value: u.mfa_enabled ? 'Ativa' : 'Desativada' },
                { label: 'Troca de senha pendente', value: u.must_change_password ? 'Sim' : 'Não' },
                { label: 'Último acesso', value: formatDateTime(u.last_login) },
                { label: 'Criado em', value: formatDateTime(u.created_at) },
              ]}
            />
            {manage ? <EditUserForm user={u} onSaved={invalidate} /> : null}
          </Card>
          <Card>
            <CardHeader>
              <div className="flex flex-col gap-1">
                <CardTitle>Perfis e escopos</CardTitle>
                <CardDescription>Cada atribuição combina um perfil com um escopo (global, empresa, estabelecimento ou cidade).</CardDescription>
              </div>
              {manage ? (
                <Button onClick={() => setAdding(true)}>
                  <Plus aria-hidden="true" />
                  Adicionar atribuição
                </Button>
              ) : null}
            </CardHeader>
            {(u.assignments ?? []).length ? (
              <div className="overflow-x-auto">
                <Table>
                  <caption className="sr-only">Atribuições do usuário</caption>
                  <TableHeader>
                    <TableRow>
                      <TableHead scope="col">Perfil</TableHead>
                      <TableHead scope="col">Escopo</TableHead>
                      {manage ? (
                        <TableHead scope="col" className="text-right">
                          Ações
                        </TableHead>
                      ) : null}
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {(u.assignments ?? []).map((a) => (
                      <TableRow key={a.id}>
                        <TableCell className="font-semibold">{labelOf(ROLE_LABELS, a.role)}</TableCell>
                        <TableCell>{scopeOf(a)}</TableCell>
                        {manage ? (
                          <TableCell className="text-right">
                            <Button variant="ghost" size="icon" aria-label={`Remover ${labelOf(ROLE_LABELS, a.role)} (${scopeOf(a)})`} onClick={() => setRemoving(a)}>
                              <Trash2 aria-hidden="true" />
                            </Button>
                          </TableCell>
                        ) : null}
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            ) : (
              <p className="text-sm text-text-muted">Sem perfis atribuídos — o usuário não acessa nenhuma área.</p>
            )}
          </Card>
        </div>
        <div className="flex flex-col gap-4 xl:col-span-4">
          {manage ? (
            <Card>
              <CardHeader>
                <div className="flex flex-col gap-1">
                  <CardTitle>Segurança da conta</CardTitle>
                  <CardDescription>Toda ação exige motivo e fica na auditoria.</CardDescription>
                </div>
              </CardHeader>
              <div className="flex flex-col gap-2">
                <Button variant={u.is_active ? 'destructive' : 'secondary'} onClick={() => setToggleActive(true)}>
                  {u.is_active ? <UserX aria-hidden="true" /> : <UserCheck aria-hidden="true" />}
                  {u.is_active ? 'Desativar usuário' : 'Reativar usuário'}
                </Button>
                <Button variant="secondary" onClick={() => setSecurity('sessions')}>
                  <LogOut aria-hidden="true" />
                  Encerrar todas as sessões
                </Button>
                {u.mfa_enabled ? (
                  <Button variant="secondary" onClick={() => setSecurity('mfa')}>
                    <KeyRound aria-hidden="true" />
                    Redefinir verificação em duas etapas
                  </Button>
                ) : null}
                {u.locked ? (
                  <Button variant="secondary" onClick={() => setSecurity('unlock')}>
                    <LockOpen aria-hidden="true" />
                    Desbloquear acesso
                  </Button>
                ) : null}
              </div>
            </Card>
          ) : (
            <Card>
              <CardHeader>
                <CardTitle>Segurança da conta</CardTitle>
              </CardHeader>
              <p className="text-sm text-text-muted">
                {isSelf
                  ? 'Para a sua própria conta use Minha conta → Segurança e Sessões.'
                  : u.is_admin_master
                    ? 'Dados de segurança do Admin Master só são alterados por ele mesmo.'
                    : 'Você não tem permissão para gerenciar este usuário (users.manage).'}
              </p>
              {isSelf ? (
                <Button asChild variant="secondary">
                  <Link to="/minha-conta/seguranca">Ir para Segurança</Link>
                </Button>
              ) : null}
            </Card>
          )}
        </div>
      </div>

      <ImpersonateDialog user={u} open={impersonating} onOpenChange={setImpersonating} />
      <AddAssignmentDialog open={adding} onOpenChange={setAdding} userId={u.id} onDone={invalidate} />
      <ConfirmDialog
        open={!!removing}
        onOpenChange={(o) => !o && setRemoving(null)}
        title={`Remover o perfil ${removing ? labelOf(ROLE_LABELS, removing.role) : ''}?`}
        description={removing ? `Escopo: ${scopeOf(removing)}. O acesso correspondente é retirado imediatamente.` : undefined}
        confirmLabel="Remover atribuição"
        destructive
        onConfirm={async () => {
          if (!removing) return
          try {
            await http.delete(`/users/${u.id}/assignments/${removing.id}`)
          } catch (e) {
            throw new Error(explainUserError(e).detail)
          }
          invalidate()
          toast.success('Atribuição removida.')
        }}
      />
      <ReasonDialog
        open={toggleActive}
        onOpenChange={setToggleActive}
        title={u.is_active ? `Desativar ${u.name}?` : `Reativar ${u.name}?`}
        consequences={u.is_active ? ['Todas as sessões do usuário são encerradas.', 'O usuário não consegue mais entrar até ser reativado.'] : ['O usuário volta a entrar com a senha atual.']}
        requireText={u.is_active ? u.name : undefined}
        reasonLabel="Observação"
        minLength={0}
        confirmLabel={u.is_active ? 'Desativar usuário' : 'Reativar usuário'}
        destructive={u.is_active}
        onConfirm={async () => {
          await http.patch(`/users/${u.id}`, { is_active: !u.is_active })
          invalidate()
          toast.success(u.is_active ? 'Usuário desativado.' : 'Usuário reativado.')
        }}
      />
      <ReasonDialog
        open={security !== null}
        onOpenChange={(o) => !o && setSecurity(null)}
        title={
          security === 'sessions'
            ? `Encerrar todas as sessões de ${u.name}?`
            : security === 'mfa'
              ? `Redefinir a verificação em duas etapas de ${u.name}?`
              : `Desbloquear o acesso de ${u.name}?`
        }
        consequences={
          security === 'sessions'
            ? ['O usuário precisará entrar novamente em todos os dispositivos.']
            : security === 'mfa'
              ? ['O 2FA e os códigos de recuperação são removidos.', 'Todas as sessões são encerradas; se o perfil exigir 2FA, ele configura de novo no próximo acesso.']
              : ['Os contadores de tentativas inválidas são zerados.']
        }
        confirmLabel={security === 'sessions' ? 'Encerrar sessões' : security === 'mfa' ? 'Redefinir 2FA' : 'Desbloquear'}
        destructive={security !== 'unlock'}
        onConfirm={async (reason) => {
          const path = security === 'sessions' ? 'sessions/revoke-all' : security === 'mfa' ? 'mfa/reset' : 'unlock'
          await http.post(`/users/${u.id}/${path}`, { reason })
          invalidate()
          toast.success(security === 'sessions' ? 'Sessões encerradas.' : security === 'mfa' ? '2FA redefinida.' : 'Acesso desbloqueado.')
        }}
      />
    </>
  )
}

function EditUserForm({ user, onSaved }: { user: UserRow; onSaved: () => void }) {
  const [name, setName] = React.useState(user.name)
  const [phone, setPhone] = React.useState(user.phone ?? '')
  const [email, setEmail] = React.useState(user.email ?? '')
  const m = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = {}
      if (name.trim() !== user.name) body.name = name.trim()
      if (phone.trim() !== (user.phone ?? '')) body.phone = phone.trim() || null
      if (email.trim() !== (user.email ?? '')) body.email = email.trim()
      return http.patch<UserRow>(`/users/${user.id}`, body)
    },
    onSuccess: () => {
      toast.success('Dados do usuário salvos.')
      onSaved()
    },
  })
  const dirty = name.trim() !== user.name || phone.trim() !== (user.phone ?? '') || email.trim() !== (user.email ?? '')
  return (
    <form
      noValidate
      className="flex flex-col gap-4 border-t border-border pt-4"
      onSubmit={(e) => {
        e.preventDefault()
        if (dirty) m.mutate()
      }}
    >
      <FieldGroup className="grid grid-cols-1 gap-5 md:grid-cols-3">
        <Field required invalid={name.trim().length < 2}>
          <FieldLabel>Nome</FieldLabel>
          <Input value={name} onChange={(e) => setName(e.target.value)} />
          <FieldError>{name.trim().length < 2 ? 'Mínimo de 2 caracteres.' : undefined}</FieldError>
        </Field>
        <Field>
          <FieldLabel>E-mail</FieldLabel>
          <Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
        </Field>
        <Field>
          <FieldLabel>Telefone</FieldLabel>
          <Input type="tel" value={phone} onChange={(e) => setPhone(e.target.value)} />
        </Field>
      </FieldGroup>
      {m.error ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">{explainUserError(m.error).detail}</AlertDescription>
        </Alert>
      ) : null}
      <div className="flex justify-end">
        <Button type="submit" loading={m.isPending} disabled={!dirty || name.trim().length < 2}>
          Salvar dados
        </Button>
      </div>
    </form>
  )
}

function AddAssignmentDialog({ open, onOpenChange, userId, onDone }: { open: boolean; onOpenChange: (o: boolean) => void; userId: string; onDone: () => void }) {
  const [rs, setRs] = React.useState<RoleScopeValue>(EMPTY_ROLE_SCOPE)
  const [submitted, setSubmitted] = React.useState(false)
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<ExplainedError | null>(null)
  React.useEffect(() => {
    if (!open) {
      setRs(EMPTY_ROLE_SCOPE)
      setSubmitted(false)
      setError(null)
    }
  }, [open])
  const { body, problem } = roleScopePayload(rs)
  return (
    <Dialog open={open} onOpenChange={(o) => (!pending ? onOpenChange(o) : undefined)}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>Adicionar atribuição</DialogTitle>
          <DialogDescription>O servidor só aceita perfis que você pode conceder no escopo escolhido.</DialogDescription>
        </DialogHeader>
        <form
          noValidate
          className="flex flex-col gap-5"
          onSubmit={async (e) => {
            e.preventDefault()
            setSubmitted(true)
            setError(null)
            if (!body) return
            setPending(true)
            try {
              await http.post(`/users/${userId}/assignments`, body)
              toast.success('Atribuição adicionada.')
              onDone()
              onOpenChange(false)
            } catch (err) {
              setError(explainUserError(err))
            } finally {
              setPending(false)
            }
          }}
        >
          <RoleScopeFields value={rs} onChange={setRs} problem={submitted ? problem : null} />
          {error ? (
            <Alert variant="danger">
              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <AlertTitle>{error.title}</AlertTitle>
                <AlertDescription className="text-text">
                  {error.detail}
                  {error.code ? <span className="mt-1 block text-xs text-text-muted">Código: {error.code}</span> : null}
                </AlertDescription>
              </div>
            </Alert>
          ) : null}
          <DialogFooter>
            <Button variant="secondary" onClick={() => onOpenChange(false)} disabled={pending}>
              Cancelar
            </Button>
            <Button type="submit" loading={pending}>
              Adicionar
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

