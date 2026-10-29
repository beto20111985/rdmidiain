/**
 * Perfis e permissões — GET /roles + GET /permissions (catálogo do contrato §3.1).
 * Perfis padrão são definidos em código (somente leitura); perfis personalizados podem
 * ser criados/alterados/removidos com `roles.manage` (o servidor recusa permissões que o
 * ator não tem: ROLE_ESCALATION).
 */
import * as React from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Check, Pencil, Plus, ShieldHalf, Trash2 } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Checkbox } from '@/components/ui/checkbox'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel, FieldLegend, FieldSet } from '@/components/ui/field'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { EmptyState, ErrorState, PageSkeleton } from '@/components/patterns/states'
import { ConfirmDialog } from '@/components/patterns/confirm-dialog'
import { useCan } from '@/hooks/usePermissions'
import { useAuthStore } from '@/stores/authStore'
import { http } from '@/lib/http'
import { isApiError } from '@/lib/errors'
import { cn } from '@/lib/utils'
import { ROLE_LABELS, SCOPE_LABELS, type ScopeType } from '@/components/admin/labels'
import type { RoleInfo } from '@/components/admin/directory'
import type { PermissionInfo } from '../types'

function groupByModule(perms: PermissionInfo[]): Array<[string, PermissionInfo[]]> {
  const map = new Map<string, PermissionInfo[]>()
  for (const p of perms) map.set(p.module, [...(map.get(p.module) ?? []), p])
  return Array.from(map.entries()).sort(([a], [b]) => a.localeCompare(b))
}

export default function RolesPage() {
  const can = useCan()
  const qc = useQueryClient()
  const roles = useQuery({ queryKey: ['roles', 'list'], queryFn: () => http.get<{ items: RoleInfo[] }>('/roles').then((r) => r.items) })
  const catalog = useQuery({
    queryKey: ['roles', 'catalog'],
    queryFn: () => http.get<{ items: PermissionInfo[] }>('/permissions').then((r) => r.items),
    staleTime: Infinity,
  })
  const [selected, setSelected] = React.useState<string | null>(null)
  const [editing, setEditing] = React.useState<RoleInfo | 'new' | null>(null)
  const [deleting, setDeleting] = React.useState<RoleInfo | null>(null)

  if (roles.isLoading || catalog.isLoading) return <PageSkeleton />
  if (roles.error || catalog.error) return <ErrorState error={roles.error ?? catalog.error} onRetry={() => void Promise.all([roles.refetch(), catalog.refetch()])} />
  const list = roles.data ?? []
  const perms = catalog.data ?? []
  const current = list.find((r) => r.slug === (selected ?? list[0]?.slug)) ?? null
  const groups = groupByModule(perms)
  const invalidate = () => void qc.invalidateQueries({ queryKey: ['roles'] })

  return (
    <>
      <PageHeader
        title="Perfis e permissões"
        description={`${list.length} perfis · ${perms.length} permissões no catálogo`}
        actions={
          can('roles.manage') ? (
            <Button onClick={() => setEditing('new')}>
              <Plus aria-hidden="true" />
              Novo perfil personalizado
            </Button>
          ) : undefined
        }
      />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-12 xl:gap-6">
        <Card className="lg:col-span-4">
          <CardHeader>
            <CardTitle>Perfis</CardTitle>
          </CardHeader>
          {list.length ? (
            <ul className="m-0 flex list-none flex-col gap-1 p-0" aria-label="Perfis">
              {list.map((r) => {
                const active = current?.slug === r.slug
                return (
                  <li key={r.id}>
                    <button
                      type="button"
                      aria-current={active ? 'true' : undefined}
                      onClick={() => setSelected(r.slug)}
                      className={cn(
                        'flex min-h-11 w-full flex-col items-start gap-0.5 rounded-md px-3 py-2 text-left transition-colors duration-fast ease-rd hover:bg-surface-hover',
                        active && 'bg-surface-hover',
                      )}
                    >
                      <span className="flex w-full flex-wrap items-center gap-2 text-sm font-semibold text-text">
                        {r.name || ROLE_LABELS[r.slug] || r.slug}
                        {r.is_system ? <Badge tone="neutral">Padrão</Badge> : <Badge tone="info">Personalizado</Badge>}
                      </span>
                      <span className="text-xs text-text-muted">
                        {r.permissions.length} permissões{r.mfa_required ? ' · 2FA obrigatório' : ''}
                      </span>
                    </button>
                  </li>
                )
              })}
            </ul>
          ) : (
            <EmptyState size="compact" headingLevel="h3" icon={ShieldHalf} title="Nenhum perfil." />
          )}
        </Card>
        <Card className="lg:col-span-8">
          {current ? (
            <>
              <CardHeader>
                <div className="flex flex-col gap-1">
                  <CardTitle>{current.name || ROLE_LABELS[current.slug] || current.slug}</CardTitle>
                  <CardDescription>
                    <code>{current.slug}</code> · {current.description ?? 'Sem descrição.'} · Escopos típicos:{' '}
                    {current.typical_scopes.length ? current.typical_scopes.map((s) => SCOPE_LABELS[s as ScopeType] ?? s).join(', ') : '—'}
                  </CardDescription>
                </div>
                {!current.is_system && can('roles.manage') ? (
                  <div className="flex gap-2">
                    <Button variant="secondary" onClick={() => setEditing(current)}>
                      <Pencil aria-hidden="true" />
                      Editar
                    </Button>
                    <Button variant="destructive" onClick={() => setDeleting(current)}>
                      <Trash2 aria-hidden="true" />
                      Remover
                    </Button>
                  </div>
                ) : null}
              </CardHeader>
              {current.is_system ? (
                <p className="text-sm text-text-muted">Perfil padrão definido em código. Para variações, crie um perfil personalizado.</p>
              ) : null}
              <div className="flex flex-col gap-5">
                {groups.map(([module, items]) => (
                  <section key={module} aria-labelledby={`mod-${module}`} className="flex flex-col gap-2">
                    <h3 id={`mod-${module}`} className="text-title-card text-text">
                      {module}
                    </h3>
                    <ul className="m-0 grid list-none grid-cols-1 gap-x-4 gap-y-1 p-0 md:grid-cols-2">
                      {items.map((p) => {
                        const has = current.permissions.includes(p.code)
                        return (
                          <li key={p.code} className={cn('flex items-start gap-2 text-sm', has ? 'text-text' : 'text-text-muted')}>
                            {has ? (
                              <Check aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-success" />
                            ) : (
                              <span aria-hidden="true" className="mt-0.5 inline-block size-4 shrink-0 text-center">
                                –
                              </span>
                            )}
                            <span>
                              {p.name} <code className="text-xs text-text-muted">{p.code}</code>
                              <span className="sr-only">{has ? ' (concedida)' : ' (não concedida)'}</span>
                            </span>
                          </li>
                        )
                      })}
                    </ul>
                  </section>
                ))}
              </div>
            </>
          ) : (
            <EmptyState size="compact" headingLevel="h3" title="Selecione um perfil." />
          )}
        </Card>
      </div>
      <RoleDialog role={editing} onOpenChange={(o) => !o && setEditing(null)} catalog={groups} onDone={invalidate} />
      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title={`Remover o perfil ${deleting?.name ?? ''}?`}
        description="Só é possível remover perfis sem usuários atribuídos."
        requireText={deleting?.slug}
        confirmLabel="Remover perfil"
        destructive
        onConfirm={async () => {
          if (!deleting) return
          await http.delete(`/roles/${deleting.id}`)
          setSelected(null)
          invalidate()
          toast.success('Perfil removido.')
        }}
      />
    </>
  )
}

function RoleDialog({
  role,
  onOpenChange,
  catalog,
  onDone,
}: {
  role: RoleInfo | 'new' | null
  onOpenChange: (o: boolean) => void
  catalog: Array<[string, PermissionInfo[]]>
  onDone: () => void
}) {
  const isNew = role === 'new'
  const existing = role && role !== 'new' ? role : null
  const myPerms = useAuthStore((s) => s.me?.permissions)
  const [slug, setSlug] = React.useState('')
  const [name, setName] = React.useState('')
  const [description, setDescription] = React.useState('')
  const [perms, setPerms] = React.useState<Set<string>>(new Set())
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<{ message: string; codes?: string[] } | null>(null)
  React.useEffect(() => {
    setSlug(existing?.slug ?? '')
    setName(existing?.name ?? '')
    setDescription(existing?.description ?? '')
    setPerms(new Set(existing?.permissions ?? []))
    setError(null)
  }, [role, existing])
  const slugOk = !isNew || /^[a-z][a-z0-9_]{2,49}$/.test(slug)
  const nameOk = name.trim().length >= 2
  const toggle = (code: string, on: boolean) =>
    setPerms((prev) => {
      const n = new Set(prev)
      if (on) n.add(code)
      else n.delete(code)
      return n
    })
  const submit = async () => {
    if (!slugOk || !nameOk) return
    setPending(true)
    setError(null)
    try {
      if (isNew) await http.post('/roles', { slug, name: name.trim(), description: description.trim() || null, permissions: [...perms] })
      else if (existing) await http.patch(`/roles/${existing.id}`, { name: name.trim(), description: description.trim(), permissions: [...perms] })
      toast.success(isNew ? 'Perfil criado.' : 'Perfil atualizado.')
      onDone()
      onOpenChange(false)
    } catch (e) {
      if (isApiError(e)) setError({ message: e.message, codes: Array.isArray(e.details.codes) ? (e.details.codes as string[]) : undefined })
      else setError({ message: String(e) })
    } finally {
      setPending(false)
    }
  }
  return (
    <Dialog open={role !== null} onOpenChange={(o) => (!pending ? onOpenChange(o) : undefined)}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>{isNew ? 'Novo perfil personalizado' : `Editar perfil ${existing?.name ?? ''}`}</DialogTitle>
          <DialogDescription>Você só pode incluir permissões que você mesmo tem em escopo global.</DialogDescription>
        </DialogHeader>
        <form
          noValidate
          className="flex flex-col gap-5"
          onSubmit={(e) => {
            e.preventDefault()
            void submit()
          }}
        >
          <FieldGroup>
            {isNew ? (
              <Field required invalid={!slugOk && slug !== ''}>
                <FieldLabel>Identificador</FieldLabel>
                <Input value={slug} onChange={(e) => setSlug(e.target.value.toLowerCase())} spellCheck={false} />
                <FieldDescription>Letras minúsculas, números e “_”; começa com letra (ex.: suporte_regional).</FieldDescription>
                <FieldError>{!slugOk && slug !== '' ? 'Identificador inválido.' : undefined}</FieldError>
              </Field>
            ) : null}
            <Field required invalid={!nameOk && name !== ''}>
              <FieldLabel>Nome</FieldLabel>
              <Input value={name} onChange={(e) => setName(e.target.value)} />
            </Field>
            <Field>
              <FieldLabel>Descrição</FieldLabel>
              <Textarea value={description} onChange={(e) => setDescription(e.target.value)} />
            </Field>
          </FieldGroup>
          <div className="flex max-h-[40vh] flex-col gap-4 overflow-y-auto rounded-lg border border-border p-3">
            {catalog.map(([module, items]) => (
              <FieldSet key={module}>
                <FieldLegend>{module}</FieldLegend>
                {items.map((p) => {
                  const mine = !!myPerms?.includes(p.code)
                  return (
                    <Field key={p.code} orientation="horizontal" className="min-h-11">
                      <Checkbox checked={perms.has(p.code)} onCheckedChange={(c) => toggle(p.code, c === true)} />
                      <FieldLabel className="font-normal">
                        {p.name} <code className="text-xs text-text-muted">{p.code}</code>
                        {!mine ? <span className="ml-1 text-xs text-warning">(você não tem)</span> : null}
                      </FieldLabel>
                    </Field>
                  )
                })}
              </FieldSet>
            ))}
          </div>
          {error ? (
            <Alert variant="danger">
              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <AlertTitle>{error.message}</AlertTitle>
                {error.codes?.length ? <AlertDescription className="text-text">Permissões recusadas: {error.codes.join(', ')}</AlertDescription> : null}
              </div>
            </Alert>
          ) : null}
          <DialogFooter>
            <Button variant="secondary" onClick={() => onOpenChange(false)} disabled={pending}>
              Cancelar
            </Button>
            <Button type="submit" loading={pending} disabled={!slugOk || !nameOk}>
              {isNew ? 'Criar perfil' : 'Salvar perfil'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
