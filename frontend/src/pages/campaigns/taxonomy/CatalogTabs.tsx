/**
 * Catálogo da taxonomia: política de conteúdo (bloqueios universais), marcas, produtos e
 * concorrência explícita entre empresas. Escrita somente RD (`taxonomy.manage`).
 */
import * as React from 'react'
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Ban, Plus, ShieldAlert } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Field, FieldError, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Skeleton } from '@/components/ui/skeleton'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { EmptyState, ErrorState } from '@/components/patterns/states'
import { ReasonDialog } from '@/components/campaigns/reason-dialog'
import { EntityPicker, type PickerOption } from '@/components/campaigns/entity-picker'
import { OrgName } from '@/components/campaigns/org-name'
import { useTableState } from '@/hooks/useTableState'
import { qk } from '@/lib/queryKeys'
import { formatDateTime } from '@/lib/format'
import { taxonomyApi } from '../lib/api'
import { searchBrands, searchOrganizations } from '../lib/pickers'
import { treeOrder } from '../lib/tree'
import type { Brand, ContentPolicy, Product } from '../lib/types'
import { describeError } from '@/pages/campaigns/lib/errorText'

// ─── Política de conteúdo ────────────────────────────────────────────────────
export function PoliciesTab({ canManage }: { canManage: boolean }) {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: qk.sub('taxonomy', 'policies'), queryFn: taxonomyApi.policies })
  const segs = useQuery({ queryKey: qk.sub('taxonomy', 'segments', { inactive: false }), queryFn: () => taxonomyApi.segments(), enabled: canManage })
  const [segment, setSegment] = React.useState('')
  const [effect, setEffect] = React.useState<'BLOCK' | 'ALLOW'>('BLOCK')
  const [setOpen, setSetOpen] = React.useState(false)
  const [removing, setRemoving] = React.useState<ContentPolicy | null>(null)
  const refresh = () => void qc.invalidateQueries({ queryKey: qk.root('taxonomy') })
  const segName = treeOrder(segs.data?.items ?? []).find((s) => s.id === segment)?.name

  return (
    <div className="flex flex-col gap-4">
      <h2 className="text-title-section">Política de conteúdo</h2>
      <Alert variant="warning">
        <ShieldAlert aria-hidden="true" />
        <div>
          <AlertTitle>Bloqueios universais não são sobrescrevíveis</AlertTitle>
          <AlertDescription>
            Apostas, conteúdo adulto e armas são proibidos em todas as telas. Um bloqueio vale para o segmento e todos os seus descendentes; um ALLOW em um filho não
            desbloqueia e nenhum override de campanha libera conteúdo proibido.
          </AlertDescription>
        </div>
      </Alert>
      {q.isLoading ? (
        <Skeleton className="h-40 w-full" />
      ) : q.error ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} size="compact" />
      ) : (
        <Table>
          <caption className="sr-only">Políticas de conteúdo</caption>
          <TableHeader>
            <TableRow className="h-11 hover:bg-transparent">
              <TableHead scope="col">Segmento</TableHead>
              <TableHead scope="col">Efeito</TableHead>
              <TableHead scope="col">Justificativa</TableHead>
              <TableHead scope="col">Atualizado</TableHead>
              {canManage ? (
                <TableHead scope="col" className="text-right">
                  Ações
                </TableHead>
              ) : null}
            </TableRow>
          </TableHeader>
          <TableBody>
            {(q.data?.items ?? []).map((p) => (
              <TableRow key={p.segment_id}>
                <TableCell className="font-semibold">{p.segment_name}</TableCell>
                <TableCell>
                  {p.effect === 'BLOCK' ? (
                    <Badge tone="danger">
                      <Ban aria-hidden="true" />
                      Bloqueado{p.is_default ? ' (padrão)' : ''}
                    </Badge>
                  ) : (
                    <Badge tone="success">Liberado{p.is_default ? ' (padrão)' : ''}</Badge>
                  )}
                </TableCell>
                <TableCell>{p.reason}</TableCell>
                <TableCell>{formatDateTime(p.updated_at)}</TableCell>
                {canManage ? (
                  <TableCell className="text-right">
                    <Button variant="ghost" onClick={() => setRemoving(p)}>
                      Remover
                    </Button>
                  </TableCell>
                ) : null}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {canManage ? (
        <div className="flex flex-wrap items-end gap-3 rounded-lg border border-border p-4">
          <Field className="w-72">
            <FieldLabel>Segmento</FieldLabel>
            <Select value={segment} onValueChange={setSegment}>
              <SelectTrigger>
                <SelectValue placeholder="Selecione" />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  {treeOrder(segs.data?.items ?? []).map((s) => (
                    <SelectItem key={s.id} value={s.id}>
                      {'— '.repeat(s.depth)}
                      {s.name}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
          </Field>
          <Field className="w-48">
            <FieldLabel>Efeito</FieldLabel>
            <Select value={effect} onValueChange={(v) => setEffect(v as 'BLOCK' | 'ALLOW')}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  <SelectItem value="BLOCK">Bloquear</SelectItem>
                  <SelectItem value="ALLOW">Liberar</SelectItem>
                </SelectGroup>
              </SelectContent>
            </Select>
          </Field>
          <Button onClick={() => setSetOpen(true)} disabled={!segment}>
            Definir política
          </Button>
        </div>
      ) : null}
      <ReasonDialog
        open={setOpen}
        onOpenChange={setSetOpen}
        title={`${effect === 'BLOCK' ? 'Bloquear' : 'Liberar'} o segmento ${segName ?? ''}?`}
        description={effect === 'BLOCK' ? 'Anúncios deste segmento e de todos os descendentes deixam de ser vendidos e exibidos.' : 'Um ALLOW não desbloqueia bloqueios de ancestrais.'}
        reasonLabel="Justificativa"
        minLength={10}
        destructive={effect === 'BLOCK'}
        confirmLabel="Salvar política"
        onConfirm={async (reason) => {
          await taxonomyApi.setPolicy(segment, effect, reason)
          toast.success('Política salva.')
          refresh()
        }}
      />
      <ReasonDialog
        open={!!removing}
        onOpenChange={(o) => !o && setRemoving(null)}
        title={`Remover a política de ${removing?.segment_name ?? ''}?`}
        description={removing?.effect === 'BLOCK' ? 'O segmento volta a seguir a política dos ancestrais (se houver).' : undefined}
        minLength={10}
        destructive
        confirmLabel="Remover política"
        onConfirm={async (reason) => {
          await taxonomyApi.removePolicy(removing!.segment_id, reason)
          toast.success('Política removida.')
          refresh()
        }}
      />
    </div>
  )
}

// ─── Marcas ──────────────────────────────────────────────────────────────────
export function BrandsTab({ canManage }: { canManage: boolean }) {
  const qc = useQueryClient()
  const table = useTableState({ defaultPageSize: 25, prefix: 'm_' })
  const params = { page: table.state.page, page_size: table.state.pageSize, search: table.state.q || undefined }
  const q = useQuery({ queryKey: qk.list('taxonomy', { brands: params }), queryFn: () => taxonomyApi.brands(params), placeholderData: keepPreviousData })
  const [editing, setEditing] = React.useState<Brand | 'new' | null>(null)
  const cols: DataTableColumn<Brand>[] = [
    { id: 'name', header: 'Marca', mobile: 'primary', cell: (b) => <span className="font-semibold">{b.name}</span> },
    { id: 'owner', header: 'Empresa dona', cell: (b) => (b.owner_org_id ? <OrgName id={b.owner_org_id} /> : '—') },
    { id: 'active', header: 'Situação', mobile: 'badge', cell: (b) => (b.is_active ? <Badge tone="success">Ativa</Badge> : <Badge>Inativa</Badge>) },
  ]
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-title-section">Marcas</h2>
        {canManage ? (
          <Button onClick={() => setEditing('new')}>
            <Plus aria-hidden="true" />
            Nova marca
          </Button>
        ) : null}
      </div>
      <DataTable
        caption="Marcas"
        columns={cols}
        data={q.data}
        getRowId={(b) => b.id}
        controller={table}
        isLoading={q.isLoading}
        isFetching={q.isFetching}
        error={q.error}
        onRetry={() => void q.refetch()}
        filtersActive={!!table.state.q}
        empty={{ title: 'Nenhuma marca cadastrada', description: 'Marcas permitem regras e exceções por marca na matriz de conflitos.' }}
        rowActions={canManage ? (b) => <Button variant="ghost" onClick={() => setEditing(b)}>Editar</Button> : undefined}
        toolbar={
          <Field className="max-w-xs">
            <FieldLabel>Buscar marca</FieldLabel>
            <Input type="search" value={table.state.q} onChange={(e) => table.setQuery(e.target.value)} />
          </Field>
        }
      />
      <BrandDialog
        brand={editing}
        onClose={() => setEditing(null)}
        onSaved={() => {
          setEditing(null)
          void qc.invalidateQueries({ queryKey: qk.root('taxonomy') })
        }}
      />
    </div>
  )
}

function BrandDialog({ brand, onClose, onSaved }: { brand: Brand | 'new' | null; onClose: () => void; onSaved: () => void }) {
  const [name, setName] = React.useState('')
  const [owner, setOwner] = React.useState<PickerOption | null>(null)
  const [active, setActive] = React.useState(true)
  const [error, setError] = React.useState<unknown>(null)
  const [pending, setPending] = React.useState(false)
  React.useEffect(() => {
    setError(null)
    if (brand && brand !== 'new') {
      setName(brand.name)
      setActive(brand.is_active)
      setOwner(brand.owner_org_id ? { id: brand.owner_org_id, label: 'Empresa atual' } : null)
    } else {
      setName('')
      setOwner(null)
      setActive(true)
    }
  }, [brand])
  if (!brand) return null
  const save = async () => {
    if (!name.trim()) return
    setPending(true)
    setError(null)
    try {
      if (brand === 'new') await taxonomyApi.createBrand({ name: name.trim(), owner_org_id: owner?.id ?? null })
      else await taxonomyApi.patchBrand(brand.id, { name: name.trim(), owner_org_id: owner?.id ?? null, is_active: active })
      toast.success('Marca salva.')
      onSaved()
    } catch (e) {
      setError(e)
    } finally {
      setPending(false)
    }
  }
  return (
    <Dialog open onOpenChange={(o) => !o && !pending && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{brand === 'new' ? 'Nova marca' : `Editar ${brand.name}`}</DialogTitle>
          <DialogDescription>A marca classifica anúncios e permite regras e exceções específicas.</DialogDescription>
        </DialogHeader>
        <Field>
          <FieldLabel required>Nome</FieldLabel>
          <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={120} />
        </Field>
        <EntityPicker label="Empresa dona (opcional)" domain="organizations" value={owner} onChange={setOwner} search={(t) => searchOrganizations(t)} />
        {brand !== 'new' ? (
          <Field>
            <FieldLabel>Situação</FieldLabel>
            <Select value={active ? 'on' : 'off'} onValueChange={(v) => setActive(v === 'on')}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  <SelectItem value="on">Ativa</SelectItem>
                  <SelectItem value="off">Inativa</SelectItem>
                </SelectGroup>
              </SelectContent>
            </Select>
          </Field>
        ) : null}
        {error ? (
          <Alert variant="danger">
            <AlertDescription>{describeError(error)}</AlertDescription>
          </Alert>
        ) : null}
        <DialogFooter>
          <Button variant="secondary" onClick={onClose} disabled={pending}>
            Cancelar
          </Button>
          <Button onClick={() => void save()} loading={pending} disabled={!name.trim()}>
            Salvar
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ─── Produtos ────────────────────────────────────────────────────────────────
export function ProductsTab({ canManage }: { canManage: boolean }) {
  const qc = useQueryClient()
  const table = useTableState({ defaultPageSize: 25, prefix: 'p_' })
  const params = { page: table.state.page, page_size: table.state.pageSize, search: table.state.q || undefined }
  const q = useQuery({ queryKey: qk.list('taxonomy', { products: params }), queryFn: () => taxonomyApi.products(params), placeholderData: keepPreviousData })
  const segs = useQuery({ queryKey: qk.sub('taxonomy', 'segments', { inactive: false }), queryFn: () => taxonomyApi.segments() })
  const segName = React.useMemo(() => Object.fromEntries((segs.data?.items ?? []).map((s) => [s.id, s.name])), [segs.data])
  const [editing, setEditing] = React.useState<Product | 'new' | null>(null)
  const cols: DataTableColumn<Product>[] = [
    { id: 'name', header: 'Produto', mobile: 'primary', cell: (p) => <span className="font-semibold">{p.name}</span> },
    { id: 'segment', header: 'Segmento', cell: (p) => segName[p.segment_id] ?? '—' },
    { id: 'active', header: 'Situação', mobile: 'badge', cell: (p) => (p.is_active ? <Badge tone="success">Ativo</Badge> : <Badge>Inativo</Badge>) },
  ]
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-title-section">Produtos</h2>
        {canManage ? (
          <Button onClick={() => setEditing('new')}>
            <Plus aria-hidden="true" />
            Novo produto
          </Button>
        ) : null}
      </div>
      <DataTable
        caption="Produtos"
        columns={cols}
        data={q.data}
        getRowId={(p) => p.id}
        controller={table}
        isLoading={q.isLoading}
        isFetching={q.isFetching}
        error={q.error}
        onRetry={() => void q.refetch()}
        filtersActive={!!table.state.q}
        empty={{ title: 'Nenhum produto cadastrado', description: 'Produtos refinam a classificação do anúncio (ex.: cerveja dentro de bebidas).' }}
        rowActions={canManage ? (p) => <Button variant="ghost" onClick={() => setEditing(p)}>Editar</Button> : undefined}
        toolbar={
          <Field className="max-w-xs">
            <FieldLabel>Buscar produto</FieldLabel>
            <Input type="search" value={table.state.q} onChange={(e) => table.setQuery(e.target.value)} />
          </Field>
        }
      />
      <ProductDialog
        product={editing}
        segments={treeOrder(segs.data?.items ?? [])}
        onClose={() => setEditing(null)}
        onSaved={() => {
          setEditing(null)
          void qc.invalidateQueries({ queryKey: qk.root('taxonomy') })
        }}
      />
    </div>
  )
}

function ProductDialog({
  product,
  segments,
  onClose,
  onSaved,
}: {
  product: Product | 'new' | null
  segments: Array<{ id: string; name: string; depth: number }>
  onClose: () => void
  onSaved: () => void
}) {
  const [name, setName] = React.useState('')
  const [segment, setSegment] = React.useState('')
  const [brand, setBrand] = React.useState<PickerOption | null>(null)
  const [active, setActive] = React.useState(true)
  const [error, setError] = React.useState<unknown>(null)
  const [pending, setPending] = React.useState(false)
  React.useEffect(() => {
    setError(null)
    if (product && product !== 'new') {
      setName(product.name)
      setSegment(product.segment_id)
      setBrand(product.brand_id ? { id: product.brand_id, label: 'Marca atual' } : null)
      setActive(product.is_active)
    } else {
      setName('')
      setSegment('')
      setBrand(null)
      setActive(true)
    }
  }, [product])
  if (!product) return null
  const save = async () => {
    setPending(true)
    setError(null)
    try {
      if (product === 'new') await taxonomyApi.createProduct({ name: name.trim(), segment_id: segment, brand_id: brand?.id ?? null })
      else await taxonomyApi.patchProduct(product.id, { name: name.trim(), segment_id: segment, brand_id: brand?.id ?? null, is_active: active })
      toast.success('Produto salvo.')
      onSaved()
    } catch (e) {
      setError(e)
    } finally {
      setPending(false)
    }
  }
  return (
    <Dialog open onOpenChange={(o) => !o && !pending && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{product === 'new' ? 'Novo produto' : `Editar ${product.name}`}</DialogTitle>
          <DialogDescription>O produto pertence a um segmento e, opcionalmente, a uma marca.</DialogDescription>
        </DialogHeader>
        <Field>
          <FieldLabel required>Nome</FieldLabel>
          <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={160} />
        </Field>
        <Field>
          <FieldLabel required>Segmento</FieldLabel>
          <Select value={segment} onValueChange={setSegment}>
            <SelectTrigger>
              <SelectValue placeholder="Selecione" />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                {segments.map((s) => (
                  <SelectItem key={s.id} value={s.id}>
                    {'— '.repeat(s.depth)}
                    {s.name}
                  </SelectItem>
                ))}
              </SelectGroup>
            </SelectContent>
          </Select>
        </Field>
        <EntityPicker label="Marca (opcional)" domain="taxonomy" value={brand} onChange={setBrand} search={searchBrands} />
        {product !== 'new' ? (
          <Field>
            <FieldLabel>Situação</FieldLabel>
            <Select value={active ? 'on' : 'off'} onValueChange={(v) => setActive(v === 'on')}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  <SelectItem value="on">Ativo</SelectItem>
                  <SelectItem value="off">Inativo</SelectItem>
                </SelectGroup>
              </SelectContent>
            </Select>
          </Field>
        ) : null}
        {error ? (
          <Alert variant="danger">
            <AlertDescription>{describeError(error)}</AlertDescription>
          </Alert>
        ) : null}
        <DialogFooter>
          <Button variant="secondary" onClick={onClose} disabled={pending}>
            Cancelar
          </Button>
          <Button onClick={() => void save()} loading={pending} disabled={!name.trim() || !segment}>
            Salvar
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ─── Concorrentes ────────────────────────────────────────────────────────────
export function CompetitorsTab({ canManage }: { canManage: boolean }) {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: qk.sub('taxonomy', 'competitors'), queryFn: taxonomyApi.competitors })
  const [a, setA] = React.useState<PickerOption | null>(null)
  const [b, setB] = React.useState<PickerOption | null>(null)
  const [formError, setFormError] = React.useState<string | null>(null)
  const [creating, setCreating] = React.useState(false)
  const [removing, setRemoving] = React.useState<string | null>(null)
  const refresh = () => void qc.invalidateQueries({ queryKey: qk.sub('taxonomy', 'competitors') })
  return (
    <div className="flex flex-col gap-4">
      <h2 className="text-title-section">Concorrência declarada entre empresas</h2>
      <p className="m-0 text-sm text-text-muted">Empresas concorrentes nunca aparecem no mesmo bloco e não anunciam no estabelecimento da outra.</p>
      {canManage ? (
        <div className="grid grid-cols-1 gap-3 rounded-lg border border-border p-4 md:grid-cols-3 md:items-end">
          <EntityPicker label="Empresa A" domain="organizations" value={a} onChange={setA} search={(t) => searchOrganizations(t)} />
          <EntityPicker label="Empresa B" domain="organizations" value={b} onChange={setB} search={(t) => searchOrganizations(t)} />
          <div className="flex flex-col gap-1">
            <Button
              onClick={() => {
                if (!a || !b) return setFormError('Escolha as duas empresas.')
                if (a.id === b.id) return setFormError('Uma empresa não concorre consigo mesma.')
                setFormError(null)
                setCreating(true)
              }}
            >
              Declarar concorrência
            </Button>
            <FieldError>{formError}</FieldError>
          </div>
        </div>
      ) : null}
      {q.isLoading ? (
        <Skeleton className="h-24 w-full" />
      ) : q.error ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} size="compact" />
      ) : !q.data?.items.length ? (
        <EmptyState size="compact" headingLevel="h3" title="Nenhuma concorrência declarada" />
      ) : (
        <Table>
          <caption className="sr-only">Concorrências</caption>
          <TableHeader>
            <TableRow className="h-11 hover:bg-transparent">
              <TableHead scope="col">Empresa A</TableHead>
              <TableHead scope="col">Empresa B</TableHead>
              <TableHead scope="col">Justificativa</TableHead>
              {canManage ? (
                <TableHead scope="col" className="text-right">
                  Ações
                </TableHead>
              ) : null}
            </TableRow>
          </TableHeader>
          <TableBody>
            {q.data.items.map((l) => (
              <TableRow key={l.id}>
                <TableCell>
                  <OrgName id={l.org_a_id} />
                </TableCell>
                <TableCell>
                  <OrgName id={l.org_b_id} />
                </TableCell>
                <TableCell>{l.reason}</TableCell>
                {canManage ? (
                  <TableCell className="text-right">
                    <Button variant="ghost" onClick={() => setRemoving(l.id)}>
                      Remover
                    </Button>
                  </TableCell>
                ) : null}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      <ReasonDialog
        open={creating}
        onOpenChange={setCreating}
        title={`Declarar ${a?.label ?? ''} e ${b?.label ?? ''} como concorrentes?`}
        minLength={10}
        reasonLabel="Justificativa"
        confirmLabel="Declarar"
        onConfirm={async (reason) => {
          await taxonomyApi.createCompetitor({ org_a_id: a!.id, org_b_id: b!.id, reason })
          toast.success('Concorrência declarada.')
          setA(null)
          setB(null)
          refresh()
        }}
      />
      <ReasonDialog
        open={!!removing}
        onOpenChange={(o) => !o && setRemoving(null)}
        title="Remover a concorrência?"
        minLength={10}
        destructive
        confirmLabel="Remover"
        onConfirm={async (reason) => {
          await taxonomyApi.removeCompetitor(removing!, reason)
          toast.success('Concorrência removida.')
          refresh()
        }}
      />
    </div>
  )
}
