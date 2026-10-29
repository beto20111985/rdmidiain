/**
 * Aba "Categorias e produtos": categoria (ramo), segmentos (múltiplos) e produtos vendidos —
 * PATCH /establishments/{id} {category_id}, PUT /segments, PUT /products. Usados pelo motor de
 * conflitos/concorrência (W-CAMP) e pela disponibilidade por categoria.
 */
import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Field, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Skeleton } from '@/components/ui/skeleton'
import { ErrorState } from '@/components/patterns/states'
import { OptionSelect } from '@/components/ops/filter-select'
import { useDebounced } from '@/components/ops/use-debounced'
import { useCan } from '@/hooks/usePermissions'
import { qk } from '@/lib/queryKeys'
import { errorMessage } from '@/lib/errors'
import { treeOrder } from '@/lib/tree'
import { establishmentsApi, taxonomyApi } from '@/pages/ops/api/client'
import { useSegments, useTaxonomyNodes } from '@/pages/ops/api/hooks'
import type { Establishment, Product } from '@/pages/ops/api/types'

export function EstablishmentTaxonomy({ est }: { est: Establishment }) {
  const can = useCan()
  const canEdit = can('establishments.manage') && est.is_active
  return (
    <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
      <CategoryCard est={est} canEdit={canEdit} />
      <SegmentsCard est={est} canEdit={canEdit} />
      <ProductsCard est={est} canEdit={canEdit} />
    </div>
  )
}

function CategoryCard({ est, canEdit }: { est: Establishment; canEdit: boolean }) {
  const qc = useQueryClient()
  const nodes = useTaxonomyNodes()
  const [value, setValue] = React.useState<string | null>(est.category_id)
  React.useEffect(() => setValue(est.category_id), [est.category_id])
  const m = useMutation({
    mutationFn: () => establishmentsApi.update(est.id, { category_id: value }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['establishments'] })
      toast.success('Categoria atualizada.')
    },
  })
  return (
    <Card>
      <CardHeader>
        <CardTitle>Categoria (ramo)</CardTitle>
        <CardDescription>Setor › categoria › subcategoria.</CardDescription>
      </CardHeader>
      <Field>
        <FieldLabel>Categoria</FieldLabel>
        <OptionSelect
          value={value}
          onChange={setValue}
          includeNone
          noneLabel="Sem categoria"
          disabled={!canEdit}
          options={treeOrder(nodes.data?.items ?? []).map((n) => ({ value: n.id, label: `${'— '.repeat(n.depth)}${n.name}` }))}
        />
      </Field>
      {m.error ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">{errorMessage(m.error)}</AlertDescription>
        </Alert>
      ) : null}
      {canEdit ? (
        <Button className="w-fit" onClick={() => m.mutate()} loading={m.isPending} disabled={value === est.category_id}>
          Salvar categoria
        </Button>
      ) : null}
    </Card>
  )
}

function SegmentsCard({ est, canEdit }: { est: Establishment; canEdit: boolean }) {
  const qc = useQueryClient()
  const segs = useSegments()
  const initial = React.useMemo(() => new Set(est.segment_ids ?? []), [est.segment_ids])
  const [sel, setSel] = React.useState<Set<string>>(initial)
  React.useEffect(() => setSel(new Set(initial)), [initial])
  const dirty = sel.size !== initial.size || [...sel].some((x) => !initial.has(x))
  const m = useMutation({
    mutationFn: () => establishmentsApi.setSegments(est.id, [...sel]),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['establishments'] })
      toast.success('Segmentos atualizados.')
    },
  })
  return (
    <Card>
      <CardHeader>
        <CardTitle>Segmentos</CardTitle>
        <CardDescription>Segmentos atendidos/vendidos pelo local (pode marcar vários).</CardDescription>
      </CardHeader>
      {segs.isLoading ? (
        <Skeleton className="h-40 w-full" />
      ) : segs.error ? (
        <ErrorState error={segs.error} onRetry={() => void segs.refetch()} size="compact" />
      ) : !segs.data?.items.length ? (
        <p className="m-0 text-sm text-text-muted">Nenhum segmento cadastrado na taxonomia.</p>
      ) : (
        <fieldset className="m-0 flex max-h-72 flex-col gap-0 overflow-y-auto border-0 p-0">
          <legend className="sr-only">Segmentos do estabelecimento</legend>
          {treeOrder(segs.data.items).map((s) => (
            <Field key={s.id} orientation="horizontal" className="min-h-11" style={{ paddingLeft: `${s.depth * 16}px` }}>
              <Checkbox
                checked={sel.has(s.id)}
                disabled={!canEdit}
                onCheckedChange={(c) =>
                  setSel((prev) => {
                    const n = new Set(prev)
                    if (c) n.add(s.id)
                    else n.delete(s.id)
                    return n
                  })
                }
              />
              <FieldLabel className="font-normal">
                {s.name}
                {s.policy === 'BLOCK' ? (
                  <Badge tone="danger" className="ml-2">
                    Conteúdo proibido
                  </Badge>
                ) : null}
              </FieldLabel>
            </Field>
          ))}
        </fieldset>
      )}
      {m.error ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">{errorMessage(m.error)}</AlertDescription>
        </Alert>
      ) : null}
      {canEdit ? (
        <Button className="w-fit" onClick={() => m.mutate()} loading={m.isPending} disabled={!dirty}>
          Salvar segmentos ({sel.size})
        </Button>
      ) : null}
    </Card>
  )
}

function ProductsCard({ est, canEdit }: { est: Establishment; canEdit: boolean }) {
  const qc = useQueryClient()
  const [search, setSearch] = React.useState('')
  const deb = useDebounced(search, 300)
  const known = React.useRef(new Map<string, Product>())
  const products = useQuery({
    queryKey: qk.sub('taxonomy', 'products', deb),
    queryFn: () => taxonomyApi.products({ search: deb || undefined, page_size: 50 }),
    staleTime: 60_000,
  })
  products.data?.items.forEach((p) => known.current.set(p.id, p))
  const initial = React.useMemo(() => new Set(est.product_ids ?? []), [est.product_ids])
  const [sel, setSel] = React.useState<Set<string>>(initial)
  React.useEffect(() => setSel(new Set(initial)), [initial])
  const dirty = sel.size !== initial.size || [...sel].some((x) => !initial.has(x))
  const m = useMutation({
    mutationFn: () => establishmentsApi.setProducts(est.id, [...sel]),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['establishments'] })
      toast.success('Produtos vendidos atualizados.')
    },
  })
  const toggle = (id: string, on: boolean) =>
    setSel((prev) => {
      const n = new Set(prev)
      if (on) n.add(id)
      else n.delete(id)
      return n
    })

  return (
    <Card className="xl:col-span-2">
      <CardHeader>
        <CardTitle>Produtos vendidos no local</CardTitle>
        <CardDescription>Usados para evitar anúncio de concorrente direto do que o estabelecimento vende.</CardDescription>
      </CardHeader>
      <div className="flex flex-wrap gap-2" aria-label="Produtos selecionados">
        {sel.size ? (
          [...sel].map((id) => (
            <Badge key={id} tone="outline">
              {known.current.get(id)?.name ?? 'Produto selecionado'}
              {canEdit ? (
                <button
                  type="button"
                  className="ml-1 rounded px-1 text-text-muted hover:text-text"
                  aria-label={`Remover ${known.current.get(id)?.name ?? 'produto'}`}
                  onClick={() => toggle(id, false)}
                >
                  ×
                </button>
              ) : null}
            </Badge>
          ))
        ) : (
          <span className="text-sm text-text-muted">Nenhum produto informado.</span>
        )}
      </div>
      {canEdit ? (
        <>
          <Field className="max-w-sm">
            <FieldLabel>Buscar produto no catálogo</FieldLabel>
            <Input value={search} onChange={(e) => setSearch(e.target.value)} />
          </Field>
          {products.isLoading ? (
            <Skeleton className="h-24 w-full" />
          ) : products.error ? (
            <ErrorState error={products.error} onRetry={() => void products.refetch()} size="compact" />
          ) : (
            <fieldset className="m-0 grid max-h-64 grid-cols-1 gap-0 overflow-y-auto border-0 p-0 sm:grid-cols-2 xl:grid-cols-3">
              <legend className="sr-only">Resultados do catálogo de produtos</legend>
              {(products.data?.items ?? []).map((p) => (
                <Field key={p.id} orientation="horizontal" className="min-h-11">
                  <Checkbox checked={sel.has(p.id)} onCheckedChange={(c) => toggle(p.id, !!c)} />
                  <FieldLabel className="font-normal">{p.name}</FieldLabel>
                </Field>
              ))}
              {!products.data?.items.length ? <p className="m-0 text-sm text-text-muted">Nenhum produto encontrado.</p> : null}
            </fieldset>
          )}
          {m.error ? (
            <Alert variant="danger">
              <AlertDescription className="text-text">{errorMessage(m.error)}</AlertDescription>
            </Alert>
          ) : null}
          <Button className="w-fit" onClick={() => m.mutate()} loading={m.isPending} disabled={!dirty}>
            Salvar produtos ({sel.size})
          </Button>
        </>
      ) : null}
    </Card>
  )
}
