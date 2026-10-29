/**
 * Lista ordenável do conteúdo interno (blocos reservados ao estabelecimento).
 * Reordenação acessível por botões "Subir"/"Descer" (teclado e leitor de tela; anúncio em região
 * live) e "Salvar ordem" explícito — sem arrastar obrigatório.
 */
import * as React from 'react'
import { ArrowDown, ArrowUp, ListVideo, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'
import { Badge } from '@/components/ui/badge'
import { EmptyState } from '@/components/patterns/states'
import { formatDuration } from '@/lib/format'
import type { InternalItem } from '@/pages/ops/api/types'
import { cn } from '@/lib/utils'

export interface InternalOrderListProps {
  items: InternalItem[]
  canReorder: boolean
  canManage: boolean
  saving?: boolean
  onSaveOrder?: (ids: string[]) => Promise<unknown> | void
  onToggle?: (item: InternalItem, active: boolean) => void
  onRemove?: (item: InternalItem) => void
  emptyText?: string
}

export function InternalOrderList({ items, canReorder, canManage, saving, onSaveOrder, onToggle, onRemove, emptyText }: InternalOrderListProps) {
  const [order, setOrder] = React.useState(items)
  const [announce, setAnnounce] = React.useState('')
  React.useEffect(() => setOrder(items), [items])
  const dirty = order.map((i) => i.id).join(',') !== items.map((i) => i.id).join(',')
  const totalS = order.filter((i) => i.active).reduce((acc, i) => acc + (i.duration_seconds ?? 0), 0)

  const move = (idx: number, delta: number) => {
    const to = idx + delta
    if (to < 0 || to >= order.length) return
    const next = [...order]
    const [it] = next.splice(idx, 1)
    next.splice(to, 0, it)
    setOrder(next)
    setAnnounce(`${it.code ?? it.name ?? 'Item'} movido para a posição ${to + 1} de ${next.length}.`)
  }

  if (!items.length)
    return (
      <EmptyState
        icon={ListVideo}
        size="compact"
        headingLevel="h3"
        title={emptyText ?? 'Nenhum conteúdo interno nesta lista.'}
        description="Sem conteúdo interno, o bloco reservado ao estabelecimento exibe o conteúdo de contingência (fallback)."
      />
    )

  return (
    <div className="flex flex-col gap-4">
      <p className="m-0 text-sm text-text-muted">
        {order.filter((i) => i.active).length} peça(s) ativa(s) · sequência de {formatDuration(totalS)}. Peças mais longas que o bloco
        interno não são exibidas; a lista roda em sequência entre os blocos.
      </p>
      <ol className="m-0 flex list-none flex-col gap-2 p-0" aria-label="Ordem do conteúdo interno">
        {order.map((it, idx) => (
          <li
            key={it.id}
            className={cn('flex flex-col gap-3 rounded-lg border border-border bg-surface p-3 sm:flex-row sm:items-center', !it.active && 'opacity-80')}
          >
            <span className="w-8 shrink-0 text-center text-sm font-semibold tabular-nums text-text-muted" aria-hidden="true">
              {idx + 1}
            </span>
            <div className="flex min-w-0 flex-1 flex-col">
              <span className="truncate font-semibold text-text">
                <span className="sr-only">Posição {idx + 1}: </span>
                {it.name ?? 'Peça'} <span className="font-normal tabular-nums text-text-muted">· {it.code ?? '—'}</span>
              </span>
              <span className="text-xs text-text-muted">
                {it.duration_seconds ? formatDuration(it.duration_seconds) : '—'}
                {!it.active ? ' · ' : ''}
                {!it.active ? <Badge tone="neutral">Desativada</Badge> : null}
              </span>
            </div>
            <div className="flex flex-wrap items-center gap-1">
              {canReorder ? (
                <>
                  <Button variant="ghost" size="icon" aria-label={`Subir ${it.name ?? it.code}`} disabled={idx === 0} onClick={() => move(idx, -1)}>
                    <ArrowUp aria-hidden="true" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label={`Descer ${it.name ?? it.code}`}
                    disabled={idx === order.length - 1}
                    onClick={() => move(idx, 1)}
                  >
                    <ArrowDown aria-hidden="true" />
                  </Button>
                </>
              ) : null}
              {canManage && onToggle ? (
                <label className="flex min-h-11 items-center gap-2 px-2 text-sm text-text">
                  <Switch checked={it.active} onCheckedChange={(c) => onToggle(it, c)} aria-label={`Ativar ${it.name ?? it.code}`} />
                  Ativa
                </label>
              ) : null}
              {canManage && onRemove ? (
                <Button variant="ghost" size="icon" aria-label={`Remover ${it.name ?? it.code} da lista`} onClick={() => onRemove(it)}>
                  <Trash2 aria-hidden="true" />
                </Button>
              ) : null}
            </div>
          </li>
        ))}
      </ol>
      <p className="sr-only" aria-live="polite">
        {announce}
      </p>
      {canReorder && onSaveOrder ? (
        <div className="flex flex-wrap gap-2">
          <Button onClick={() => void onSaveOrder(order.map((i) => i.id))} disabled={!dirty} loading={saving}>
            Salvar ordem
          </Button>
          <Button variant="secondary" onClick={() => setOrder(items)} disabled={!dirty || saving}>
            Desfazer alterações
          </Button>
        </div>
      ) : null}
    </div>
  )
}
