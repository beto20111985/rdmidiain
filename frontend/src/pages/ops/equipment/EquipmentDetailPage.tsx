/**
 * Item do estoque: dados, local atual (itens com série) ou SALDOS DERIVADOS por local (itens por
 * quantidade), vínculo a estabelecimento/tela/comodato e histórico append-only de movimentações.
 * GET/PATCH /equipment/{id}, GET/POST /equipment/{id}/movements.
 */
import * as React from 'react'
import { useParams } from 'react-router-dom'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { ArrowLeftRight, Pencil } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Can } from '@/components/auth/guards'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Skeleton } from '@/components/ui/skeleton'
import { Field, FieldLabel } from '@/components/ui/field'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { EmptyState, ErrorState, NoPermission, PageSkeleton } from '@/components/patterns/states'
import { DetailList } from '@/components/ops/detail-list'
import { LocationLabel } from '@/components/ops/location-label'
import { OptionSelect } from '@/components/ops/filter-select'
import { UserName } from '@/components/ops/entity-name'
import { RealtimeStale } from '@/components/ops/realtime-stale'
import { emptyToNull } from '@/components/ops/utils'
import { errorMessage, isApiError } from '@/lib/errors'
import { qk } from '@/lib/queryKeys'
import { formatCents, formatDate, formatDateTime, formatNumber } from '@/lib/format'
import { equipmentApi } from '@/pages/ops/api/client'
import { CONDITION_LABEL, EQUIPMENT_STATUS_LABEL, EQUIPMENT_TYPE_LABEL, MOVEMENT_TYPE_LABEL, labelOf } from '@/pages/ops/api/labels'
import type { EquipmentItem } from '@/pages/ops/api/types'
import { MovementDialog } from './MovementDialog'

const d = (v: string | null) => (v ? formatDate(`${v}T12:00:00Z`) : null)

export default function EquipmentDetailPage() {
  const { id = '' } = useParams()
  const [page, setPage] = React.useState(1)
  const [moveOpen, setMoveOpen] = React.useState(false)
  const [editOpen, setEditOpen] = React.useState(false)
  const q = useQuery({ queryKey: qk.detail('equipment', id), queryFn: () => equipmentApi.get(id) })
  const mv = useQuery({
    queryKey: qk.sub('equipment', 'movements', id, page),
    queryFn: () => equipmentApi.movements(id, page),
    placeholderData: keepPreviousData,
  })
  const it = q.data
  const crumbs = React.useMemo(() => [{ label: 'Estoque', to: '/estoque' }, { label: it?.name ?? 'Item' }], [it?.name])

  if (q.isLoading) return <PageSkeleton />
  if (isApiError(q.error) && q.error.isForbidden) return <NoPermission permission="equipment.view" />
  if (q.error || !it)
    return (
      <ErrorState
        error={q.error}
        title={isApiError(q.error) && q.error.isNotFound ? 'Item não encontrado ou fora do seu acesso' : undefined}
        onRetry={() => void q.refetch()}
      />
    )
  const pages = Math.max(1, Math.ceil((mv.data?.total ?? 0) / 25))

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title={it.name}
        description={`${labelOf(EQUIPMENT_TYPE_LABEL, it.type)} · ${it.tracking === 'SERIAL' ? 'controle por nº de série' : 'controle por quantidade'}`}
        breadcrumbs={crumbs}
        actions={
          <Can permission="equipment.manage">
            <div className="flex flex-wrap gap-2">
              <Button variant="secondary" onClick={() => setEditOpen(true)}>
                <Pencil aria-hidden="true" />
                Editar dados
              </Button>
              <Button onClick={() => setMoveOpen(true)} disabled={it.status === 'WRITTEN_OFF'}>
                <ArrowLeftRight aria-hidden="true" />
                Movimentar
              </Button>
            </div>
          </Can>
        }
      />
      <RealtimeStale updatedAt={q.dataUpdatedAt} onRefresh={() => { void q.refetch(); void mv.refetch() }} />
      <div className="grid grid-cols-1 gap-6 xl:grid-cols-12">
        <Card className="xl:col-span-7">
          <CardHeader>
            <CardTitle>Dados do item</CardTitle>
            <Badge tone={it.status === 'IN_STOCK' ? 'success' : it.status === 'WRITTEN_OFF' ? 'neutral' : 'info'}>{labelOf(EQUIPMENT_STATUS_LABEL, it.status)}</Badge>
          </CardHeader>
          <DetailList
            items={[
              { label: 'Marca/modelo', value: [it.brand, it.model].filter(Boolean).join(' ') || null },
              { label: 'Nº de série', value: it.serial_number },
              { label: 'Patrimônio', value: it.asset_tag },
              { label: 'Polegadas', value: it.inches, numeric: true },
              { label: 'Conservação', value: labelOf(CONDITION_LABEL, it.condition) },
              { label: 'Unidade', value: it.unit },
              { label: 'Fornecedor', value: it.supplier },
              { label: 'Compra', value: d(it.purchase_date) },
              { label: 'Custo de compra', value: formatCents(it.purchase_cost_cents), numeric: true },
              { label: 'Garantia até', value: d(it.warranty_until) },
              { label: 'Devolução prevista', value: d(it.expected_return_at) },
              { label: 'Observações', value: it.notes, wide: true },
            ]}
          />
        </Card>
        <Card className="xl:col-span-5">
          <CardHeader>
            <CardTitle>{it.tracking === 'SERIAL' ? 'Local atual' : 'Saldos por local'}</CardTitle>
          </CardHeader>
          {it.tracking === 'SERIAL' && it.location ? (
            <div className="flex flex-col gap-2 text-sm">
              <LocationLabel
                kind={it.location.kind}
                establishmentId={it.location.establishment_id}
                screenId={it.location.screen_id}
                userId={it.location.user_id}
                cityKey={it.location.city_key}
              />
              {it.comodato_agreement_id ? <span className="text-text-muted">Vinculado a acordo de comodato.</span> : null}
            </div>
          ) : (
            <ul className="m-0 flex list-none flex-col divide-y divide-border p-0 text-sm">
              {(it.balances ?? []).length ? (
                (it.balances ?? []).map((b) => (
                  <li key={`${b.kind}-${b.ref_id}-${b.city_key}`} className="flex items-center justify-between gap-2 py-2">
                    <LocationLabel kind={b.kind} refId={b.ref_id} cityKey={b.city_key} />
                    <span className="tabular-nums font-semibold">{formatNumber(b.quantity)}</span>
                  </li>
                ))
              ) : (
                <li className="py-2 text-text-muted">Sem saldo em nenhum local.</li>
              )}
            </ul>
          )}
          <CardDescription>
            Saldo derivado das movimentações (entradas − saídas). Total: {formatNumber(it.total_quantity ?? null)} {it.unit}
          </CardDescription>
        </Card>
        <Card className="xl:col-span-12">
          <CardHeader>
            <div className="flex flex-col gap-1">
              <CardTitle>Movimentações</CardTitle>
              <CardDescription>Histórico imutável (append-only), da mais recente para a mais antiga.</CardDescription>
            </div>
          </CardHeader>
          {mv.isLoading ? (
            <Skeleton className="h-40 w-full" />
          ) : mv.error && !mv.data ? (
            <ErrorState error={mv.error} onRetry={() => void mv.refetch()} size="compact" />
          ) : !mv.data?.items.length ? (
            <EmptyState size="compact" headingLevel="h3" title="Nenhuma movimentação." />
          ) : (
            <>
              <ol className="m-0 flex list-none flex-col divide-y divide-border p-0">
                {mv.data.items.map((m) => (
                  <li key={m.id} className="flex flex-col gap-1 py-3 text-sm">
                    <div className="flex flex-wrap items-baseline justify-between gap-2">
                      <span className="font-semibold text-text">
                        #{m.seq} · {labelOf(MOVEMENT_TYPE_LABEL, m.movement_type)}
                        {it.tracking === 'BULK' ? ` · ${formatNumber(m.quantity)} ${it.unit}` : ''}
                      </span>
                      <time className="text-xs text-text-muted" dateTime={m.created_at ?? undefined}>
                        {formatDateTime(m.created_at)}
                      </time>
                    </div>
                    <span className="text-text-muted">
                      De <LocationLabel kind={m.from.kind} refId={m.from.ref_id} cityKey={m.from.city_key} /> para{' '}
                      <LocationLabel kind={m.to.kind} refId={m.to.ref_id} cityKey={m.to.city_key} />
                    </span>
                    {m.reason ? <span className="text-text">Justificativa: {m.reason}</span> : null}
                    <span className="text-xs text-text-muted">
                      Por <UserName id={m.performed_by} />
                      {m.reference ? ` · ref. ${m.reference}` : ''}
                      {m.expected_return_at ? ` · devolução prevista ${d(m.expected_return_at)}` : ''}
                    </span>
                  </li>
                ))}
              </ol>
              {pages > 1 ? (
                <nav aria-label="Paginação das movimentações" className="flex items-center justify-end gap-2 text-sm text-text-muted">
                  <Button variant="secondary" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
                    Anterior
                  </Button>
                  <span aria-current="page">
                    Página {page} de {pages}
                  </span>
                  <Button variant="secondary" disabled={page >= pages} onClick={() => setPage((p) => p + 1)}>
                    Próxima
                  </Button>
                </nav>
              ) : null}
            </>
          )}
        </Card>
      </div>
      <MovementDialog item={it} open={moveOpen} onOpenChange={setMoveOpen} />
      <EditDialog item={it} open={editOpen} onOpenChange={setEditOpen} />
    </div>
  )
}

function EditDialog({ item, open, onOpenChange }: { item: EquipmentItem; open: boolean; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient()
  const [v, setV] = React.useState(() => ({ ...item }))
  React.useEffect(() => {
    if (open) setV({ ...item })
  }, [open, item])
  const m = useMutation({
    mutationFn: () =>
      equipmentApi.update(item.id, {
        name: v.name.trim(),
        brand: emptyToNull(v.brand),
        model: emptyToNull(v.model),
        ...(item.tracking === 'SERIAL' ? { serial_number: emptyToNull(v.serial_number), asset_tag: emptyToNull(v.asset_tag) } : {}),
        condition: v.condition,
        supplier: emptyToNull(v.supplier),
        notes: emptyToNull(v.notes),
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['equipment'] })
      toast.success('Dados do item atualizados.')
      onOpenChange(false)
    },
  })
  return (
    <Dialog open={open} onOpenChange={(o) => (!m.isPending ? onOpenChange(o) : undefined)}>
      <DialogContent size="md">
        <form
          noValidate
          className="flex flex-col gap-5"
          onSubmit={(e) => {
            e.preventDefault()
            if (v.name.trim().length >= 2) m.mutate()
          }}
        >
          <DialogHeader>
            <DialogTitle>Editar dados do item</DialogTitle>
          </DialogHeader>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <Field invalid={v.name.trim().length < 2} required className="sm:col-span-2">
              <FieldLabel>Nome</FieldLabel>
              <Input value={v.name} onChange={(e) => setV((s) => ({ ...s, name: e.target.value }))} />
            </Field>
            <Field>
              <FieldLabel>Marca</FieldLabel>
              <Input value={v.brand ?? ''} onChange={(e) => setV((s) => ({ ...s, brand: e.target.value }))} />
            </Field>
            <Field>
              <FieldLabel>Modelo</FieldLabel>
              <Input value={v.model ?? ''} onChange={(e) => setV((s) => ({ ...s, model: e.target.value }))} />
            </Field>
            {item.tracking === 'SERIAL' ? (
              <>
                <Field>
                  <FieldLabel>Nº de série</FieldLabel>
                  <Input value={v.serial_number ?? ''} onChange={(e) => setV((s) => ({ ...s, serial_number: e.target.value }))} />
                </Field>
                <Field>
                  <FieldLabel>Patrimônio</FieldLabel>
                  <Input value={v.asset_tag ?? ''} onChange={(e) => setV((s) => ({ ...s, asset_tag: e.target.value }))} />
                </Field>
              </>
            ) : null}
            <Field>
              <FieldLabel>Conservação</FieldLabel>
              <OptionSelect
                value={v.condition}
                onChange={(x) => x && setV((s) => ({ ...s, condition: x as EquipmentItem['condition'] }))}
                options={Object.entries(CONDITION_LABEL).map(([value, label]) => ({ value, label }))}
              />
            </Field>
            <Field>
              <FieldLabel>Fornecedor</FieldLabel>
              <Input value={v.supplier ?? ''} onChange={(e) => setV((s) => ({ ...s, supplier: e.target.value }))} />
            </Field>
            <Field className="sm:col-span-2">
              <FieldLabel>Observações</FieldLabel>
              <Textarea value={v.notes ?? ''} onChange={(e) => setV((s) => ({ ...s, notes: e.target.value }))} rows={2} />
            </Field>
          </div>
          {m.error ? (
            <Alert variant="danger">
              <AlertDescription className="text-text">{errorMessage(m.error)}</AlertDescription>
            </Alert>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)} disabled={m.isPending}>
              Cancelar
            </Button>
            <Button type="submit" loading={m.isPending}>
              Salvar
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
