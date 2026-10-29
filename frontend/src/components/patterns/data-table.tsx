/**
 * DataTable RD (DS §10 Tabelas):
 * - cabeçalho 44 px, linhas ≥ 52 px, números à direita, status por texto + badge;
 * - ordenação visível e anunciada (aria-sort + região live);
 * - paginação do contrato v1 (`page`/`page_size`/`total`), com estado na URL via
 *   useTableState → filtros/página preservados ao abrir detalhes e voltar;
 * - mobile (< 768 px): cartões resumidos com as MESMAS ações (nada essencial escondido);
 * - estados: esqueleto (carga inicial), erro com tentar novamente (filtros mantidos),
 *   vazio com explicação/ação, vazio filtrado com "Limpar filtros", recarga mantendo a
 *   tabela anterior com opacidade reduzida.
 */
import * as React from 'react'
import { ArrowDown, ArrowUp, ArrowUpDown, ChevronLeft, ChevronRight, SearchX } from 'lucide-react'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { EmptyState, ErrorState, type EmptyStateProps } from './states'
import { useIsMobile } from '@/hooks/useMediaQuery'
import { PAGE_SIZES, type useTableState } from '@/hooks/useTableState'
import type { Paginated } from '@/lib/http'
import { formatNumber } from '@/lib/format'
import { cn } from '@/lib/utils'

export interface DataTableColumn<T> {
  id: string
  header: string
  cell: (row: T) => React.ReactNode
  /** Campo de ordenação na API (habilita ordenar por esta coluna). */
  sortField?: string
  numeric?: boolean
  /** Papel no cartão mobile: título, subtítulo, badge, campo (rótulo: valor) ou oculto. */
  mobile?: 'primary' | 'secondary' | 'badge' | 'field' | 'hidden'
  className?: string
}

type Controller = ReturnType<typeof useTableState>

export interface DataTableProps<T> {
  /** Nome acessível da tabela (caption). */
  caption: string
  columns: DataTableColumn<T>[]
  data: Paginated<T> | undefined
  getRowId: (row: T) => string
  controller: Controller
  isLoading?: boolean
  isFetching?: boolean
  error?: unknown
  onRetry?: () => void
  rowActions?: (row: T) => React.ReactNode
  /** Vazio sem filtros: explique e ofereça ação útil (DS §10/§17). */
  empty: Pick<EmptyStateProps, 'title' | 'description' | 'action' | 'icon'>
  /** Há busca/filtros ativos (muda a mensagem de vazio). */
  filtersActive?: boolean
  toolbar?: React.ReactNode
  className?: string
}

function sortStateFor(sort: string | null, field?: string): 'ascending' | 'descending' | 'none' {
  if (!field || !sort) return 'none'
  if (sort === field) return 'ascending'
  if (sort === `-${field}`) return 'descending'
  return 'none'
}

export function DataTable<T>({
  caption,
  columns,
  data,
  getRowId,
  controller,
  isLoading,
  isFetching,
  error,
  onRetry,
  rowActions,
  empty,
  filtersActive,
  toolbar,
  className,
}: DataTableProps<T>) {
  const isMobile = useIsMobile()
  const { state } = controller
  const [announce, setAnnounce] = React.useState('')
  const pageSizeLabelId = React.useId()
  const total = data?.total ?? 0
  const pageCount = Math.max(1, Math.ceil(total / (data?.page_size ?? state.pageSize)))
  const from = total === 0 ? 0 : (state.page - 1) * state.pageSize + 1
  const to = Math.min(total, state.page * state.pageSize)
  const rows = data?.items ?? []
  const stale = Boolean(isFetching && !isLoading)

  const onSort = (col: DataTableColumn<T>) => {
    if (!col.sortField) return
    const current = sortStateFor(state.sort, col.sortField)
    const next = current === 'none' ? 'crescente' : current === 'ascending' ? 'decrescente' : 'padrão'
    controller.toggleSort(col.sortField)
    setAnnounce(next === 'padrão' ? 'Ordenação padrão restaurada.' : `Ordenado por ${col.header}, ${next}.`)
  }

  let body: React.ReactNode
  if (isLoading) {
    body = (
      <div role="status" className="flex flex-col gap-2 p-1">
        <span className="sr-only">Carregando {caption}…</span>
        {Array.from({ length: 5 }).map((_, i) => (
          <Skeleton key={i} className="h-[52px] w-full" />
        ))}
      </div>
    )
  } else if (error && !data) {
    body = <ErrorState error={error} onRetry={onRetry} size="compact" />
  } else if (rows.length === 0) {
    body = filtersActive ? (
      <EmptyState
        size="compact"
        headingLevel="h3"
        icon={SearchX}
        title="Nenhum resultado para os filtros aplicados"
        description="Revise a busca ou limpe os filtros para ver todos os registros."
        action={
          <Button variant="secondary" onClick={controller.clearFilters}>
            Limpar filtros
          </Button>
        }
      />
    ) : (
      <EmptyState size="compact" headingLevel="h3" {...empty} />
    )
  } else if (isMobile) {
    const primary = columns.find((c) => c.mobile === 'primary') ?? columns[0]
    const secondary = columns.filter((c) => c.mobile === 'secondary')
    const badges = columns.filter((c) => c.mobile === 'badge')
    const fields = columns.filter((c) => c.mobile === 'field' || (c.mobile === undefined && c !== primary))
    body = (
      <ul aria-label={caption} className="m-0 flex list-none flex-col divide-y divide-border p-0">
        {rows.map((row) => (
          <li key={getRowId(row)} className="flex flex-col gap-3 py-4 first:pt-0 last:pb-0">
            <div className="flex items-start justify-between gap-3">
              <div className="flex min-w-0 flex-col gap-0.5">
                <div className="min-w-0 font-semibold text-text">{primary.cell(row)}</div>
                {secondary.map((c) => (
                  <div key={c.id} className="text-xs text-text-muted">
                    {c.cell(row)}
                  </div>
                ))}
              </div>
              {badges.length ? <div className="flex shrink-0 flex-col items-end gap-1">{badges.map((c) => <div key={c.id}>{c.cell(row)}</div>)}</div> : null}
            </div>
            {fields.length ? (
              <dl className="m-0 grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
                {fields.map((c) => (
                  <div key={c.id} className="flex min-w-0 flex-col">
                    <dt className="text-xs text-text-muted">{c.header}</dt>
                    <dd className={cn('m-0 min-w-0 text-text', c.numeric && 'tabular-nums')}>{c.cell(row)}</dd>
                  </div>
                ))}
              </dl>
            ) : null}
            {rowActions ? <div className="flex flex-wrap gap-2 border-t border-border pt-3">{rowActions(row)}</div> : null}
          </li>
        ))}
      </ul>
    )
  } else {
    body = (
      <Table>
        <caption className="sr-only">{caption}</caption>
        <TableHeader>
          <TableRow className="h-11 hover:bg-transparent">
            {columns.map((col) => {
              const s = sortStateFor(state.sort, col.sortField)
              return (
                <TableHead key={col.id} scope="col" numeric={col.numeric} aria-sort={col.sortField ? s : undefined} className={col.className}>
                  {col.sortField ? (
                    <button
                      type="button"
                      onClick={() => onSort(col)}
                      className={cn(
                        '-mx-2 inline-flex min-h-11 items-center gap-1.5 rounded-md px-2 font-semibold hover:text-text',
                        s !== 'none' && 'text-text',
                        col.numeric && 'flex-row-reverse',
                      )}
                    >
                      {col.header}
                      {s === 'ascending' ? (
                        <ArrowUp aria-hidden="true" className="size-4" />
                      ) : s === 'descending' ? (
                        <ArrowDown aria-hidden="true" className="size-4" />
                      ) : (
                        <ArrowUpDown aria-hidden="true" className="size-4 opacity-60" />
                      )}
                    </button>
                  ) : (
                    col.header
                  )}
                </TableHead>
              )
            })}
            {rowActions ? (
              <TableHead scope="col" className="text-right">
                Ações
              </TableHead>
            ) : null}
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row) => (
            <TableRow key={getRowId(row)}>
              {columns.map((col) => (
                <TableCell key={col.id} numeric={col.numeric} className={col.className}>
                  {col.cell(row)}
                </TableCell>
              ))}
              {rowActions ? (
                <TableCell className="text-right">
                  <div className="flex justify-end gap-1">{rowActions(row)}</div>
                </TableCell>
              ) : null}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    )
  }

  const showPagination = !isLoading && !(error && !data) && total > 0

  return (
    <div className={cn('flex min-w-0 flex-col gap-4', className)}>
      {toolbar ? <div className="flex flex-wrap items-end gap-3">{toolbar}</div> : null}
      {error && data ? <ErrorState error={error} onRetry={onRetry} size="compact" title="Não foi possível atualizar a lista" /> : null}
      <div aria-busy={stale || isLoading || undefined} className={cn('min-w-0 transition-opacity duration-fast ease-rd', stale && 'opacity-60')}>
        {body}
      </div>
      <p className="sr-only" aria-live="polite">
        {announce}
      </p>
      {showPagination ? (
        <nav aria-label={`Paginação de ${caption}`} className="flex flex-wrap items-center justify-between gap-3 text-sm text-text-muted">
          <p className="tabular-nums">
            {formatNumber(from)}–{formatNumber(to)} de {formatNumber(total)}
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <div className="flex items-center gap-2">
              <span id={pageSizeLabelId} className="whitespace-nowrap">
                Itens por página
              </span>
              <Select value={String(state.pageSize)} onValueChange={(v) => controller.setPageSize(Number(v))}>
                <SelectTrigger aria-labelledby={pageSizeLabelId} className="w-[84px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    {PAGE_SIZES.map((s) => (
                      <SelectItem key={s} value={String(s)}>
                        {s}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
            </div>
            <Button
              variant="ghost"
              size="icon"
              aria-label="Página anterior"
              disabled={state.page <= 1}
              onClick={() => controller.setPage(state.page - 1)}
            >
              <ChevronLeft aria-hidden="true" />
            </Button>
            <span className="tabular-nums" aria-current="page">
              Página {state.page} de {pageCount}
            </span>
            <Button
              variant="ghost"
              size="icon"
              aria-label="Próxima página"
              disabled={state.page >= pageCount}
              onClick={() => controller.setPage(state.page + 1)}
            >
              <ChevronRight aria-hidden="true" />
            </Button>
          </div>
        </nav>
      ) : null}
    </div>
  )
}
