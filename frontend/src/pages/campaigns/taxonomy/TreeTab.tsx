/**
 * Árvore de taxonomia (Setor > Categoria > Subcategoria de estabelecimentos; segmentos/produtos
 * dos anúncios): criar, renomear, ativar/desativar e mover subárvore (ciclos são recusados pelo
 * servidor). Escrita: `taxonomy.manage` (somente RD); toda alteração é auditada no servidor.
 */
import * as React from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Ban, Pencil, Plus } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState, ErrorState } from '@/components/patterns/states'
import { qk } from '@/lib/queryKeys'
import { taxonomyApi } from '../lib/api'
import { treeOrder } from '../lib/tree'
import type { TreeItem } from '../lib/types'
import { describeError } from '@/pages/campaigns/lib/errorText'
import { TAXONOMY_KIND_LABEL } from '../lib/labels'

const KIND_LABEL = TAXONOMY_KIND_LABEL
const CHILD_KIND: Record<string, string> = { SECTOR: 'CATEGORY', CATEGORY: 'SUBCATEGORY' }

type Editing = { mode: 'create'; parent: TreeItem | null } | { mode: 'edit'; item: TreeItem }

export function TreeTab({ variant, canManage }: { variant: 'nodes' | 'segments'; canManage: boolean }) {
  const qc = useQueryClient()
  const [showInactive, setShowInactive] = React.useState(false)
  const [editing, setEditing] = React.useState<Editing | null>(null)
  const q = useQuery({
    queryKey: qk.sub('taxonomy', variant, { inactive: showInactive }),
    queryFn: () => (variant === 'nodes' ? taxonomyApi.nodes(showInactive) : taxonomyApi.segments(showInactive)),
  })
  const items = treeOrder(q.data?.items ?? [])
  const title = variant === 'nodes' ? 'Setores, categorias e subcategorias de estabelecimentos' : 'Segmentos e produtos (classificação dos anúncios)'

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-title-section">{title}</h2>
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex min-h-11 items-center gap-2 text-sm">
            <Switch checked={showInactive} onCheckedChange={setShowInactive} aria-label="Mostrar inativos" />
            Mostrar inativos
          </label>
          {canManage ? (
            <Button onClick={() => setEditing({ mode: 'create', parent: null })}>
              <Plus aria-hidden="true" />
              {variant === 'nodes' ? 'Novo setor' : 'Novo segmento'}
            </Button>
          ) : null}
        </div>
      </div>
      {q.isLoading ? (
        <Skeleton className="h-48 w-full" />
      ) : q.error ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} size="compact" />
      ) : items.length === 0 ? (
        <EmptyState size="compact" headingLevel="h3" title="Nada cadastrado ainda" description="Crie a primeira entrada da árvore." />
      ) : (
        <ul className="m-0 flex list-none flex-col p-0" aria-label={title}>
          {items.map((it) => (
            <li
              key={it.id}
              className="flex min-h-12 flex-wrap items-center justify-between gap-2 border-b border-border py-1"
              style={{ paddingLeft: `${it.depth * 24}px` }}
            >
              <span className="flex flex-wrap items-center gap-2 text-sm">
                {it.kind ? <span className="text-xs text-text-muted">{KIND_LABEL[it.kind]}</span> : null}
                <span className={it.is_active ? 'font-semibold' : 'text-text-muted line-through'}>{it.name}</span>
                {!it.is_active ? <Badge tone="neutral">Inativo</Badge> : null}
                {it.policy === 'BLOCK' ? (
                  <Badge tone="danger">
                    <Ban aria-hidden="true" />
                    Conteúdo proibido
                  </Badge>
                ) : it.policy === 'ALLOW' ? (
                  <Badge tone="success">Liberado (política)</Badge>
                ) : null}
              </span>
              {canManage ? (
                <span className="flex gap-1">
                  {variant === 'segments' || (it.kind && CHILD_KIND[it.kind]) ? (
                    <Button variant="ghost" onClick={() => setEditing({ mode: 'create', parent: it })} aria-label={`Adicionar dentro de ${it.name}`}>
                      <Plus aria-hidden="true" />
                      Filho
                    </Button>
                  ) : null}
                  <Button variant="ghost" size="icon" onClick={() => setEditing({ mode: 'edit', item: it })} aria-label={`Editar ${it.name}`}>
                    <Pencil aria-hidden="true" />
                  </Button>
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      <TreeItemDialog
        variant={variant}
        editing={editing}
        all={q.data?.items ?? []}
        onClose={() => setEditing(null)}
        onSaved={() => {
          setEditing(null)
          void qc.invalidateQueries({ queryKey: qk.root('taxonomy') })
        }}
      />
    </div>
  )
}

function TreeItemDialog({
  variant,
  editing,
  all,
  onClose,
  onSaved,
}: {
  variant: 'nodes' | 'segments'
  editing: Editing | null
  all: TreeItem[]
  onClose: () => void
  onSaved: () => void
}) {
  const [name, setName] = React.useState('')
  const [active, setActive] = React.useState(true)
  const [parent, setParent] = React.useState<string>('__keep')
  const [error, setError] = React.useState<string | null>(null)
  const [serverError, setServerError] = React.useState<unknown>(null)
  const [pending, setPending] = React.useState(false)

  React.useEffect(() => {
    setError(null)
    setServerError(null)
    if (editing?.mode === 'edit') {
      setName(editing.item.name)
      setActive(editing.item.is_active)
      setParent('__keep')
    } else {
      setName('')
      setActive(true)
    }
  }, [editing])

  if (!editing) return null
  const item = editing.mode === 'edit' ? editing.item : null
  const kind = editing.mode === 'create' ? (editing.parent?.kind ? CHILD_KIND[editing.parent.kind] : 'SECTOR') : item?.kind
  // destinos válidos para mover: não pode ser o próprio nem um descendente
  const moveTargets = item ? all.filter((x) => x.id !== item.id && !x.ancestor_ids.includes(item.id)) : []

  const save = async (e: React.FormEvent) => {
    e.preventDefault()
    if (name.trim().length < 2) {
      setError('Mínimo de 2 caracteres.')
      return
    }
    setPending(true)
    setServerError(null)
    try {
      if (editing.mode === 'create') {
        if (variant === 'nodes') await taxonomyApi.createNode({ kind: kind ?? 'SECTOR', name: name.trim(), parent_id: editing.parent?.id ?? null })
        else await taxonomyApi.createSegment({ name: name.trim(), parent_id: editing.parent?.id ?? null })
      } else if (item) {
        const body: Record<string, unknown> = {}
        if (name.trim() !== item.name) body.name = name.trim()
        if (active !== item.is_active) body.is_active = active
        if (parent === '__root') body.move_to_root = true
        else if (parent !== '__keep') body.parent_id = parent
        if (Object.keys(body).length) {
          if (variant === 'nodes') await taxonomyApi.patchNode(item.id, body)
          else await taxonomyApi.patchSegment(item.id, body)
        }
      }
      toast.success('Taxonomia atualizada.')
      onSaved()
    } catch (err) {
      setServerError(err)
    } finally {
      setPending(false)
    }
  }

  return (
    <Dialog open onOpenChange={(o) => !o && !pending && onClose()}>
      <DialogContent>
        <form onSubmit={save} className="flex flex-col gap-4" noValidate>
          <DialogHeader>
            <DialogTitle>
              {editing.mode === 'create'
                ? `Novo ${variant === 'nodes' ? (KIND_LABEL[kind ?? 'SECTOR'] ?? 'item').toLowerCase() : 'segmento'}${editing.parent ? ` em ${editing.parent.name}` : ''}`
                : `Editar ${item?.name}`}
            </DialogTitle>
            <DialogDescription>Alterações valem imediatamente para conflitos e segmentação e ficam na auditoria.</DialogDescription>
          </DialogHeader>
          <Field invalid={!!error}>
            <FieldLabel required>Nome</FieldLabel>
            <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={120} />
            <FieldError>{error}</FieldError>
          </Field>
          {item ? (
            <>
              <Field orientation="horizontal" className="min-h-11">
                <Switch checked={active} onCheckedChange={setActive} />
                <FieldLabel className="font-normal">Ativo</FieldLabel>
              </Field>
              <Field>
                <FieldLabel>Mover para</FieldLabel>
                <Select value={parent} onValueChange={setParent}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      <SelectItem value="__keep">Manter posição atual</SelectItem>
                      <SelectItem value="__root">Raiz (nível superior)</SelectItem>
                      {moveTargets.map((t) => (
                        <SelectItem key={t.id} value={t.id}>
                          {'— '.repeat(t.depth)}
                          {t.name}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  </SelectContent>
                </Select>
                <FieldDescription>Move a subárvore inteira. O servidor recusa ciclos e níveis inválidos.</FieldDescription>
              </Field>
            </>
          ) : null}
          {serverError ? (
            <Alert variant="danger">
              <AlertDescription>{describeError(serverError)}</AlertDescription>
            </Alert>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={onClose} disabled={pending}>
              Cancelar
            </Button>
            <Button type="submit" loading={pending}>
              Salvar
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
