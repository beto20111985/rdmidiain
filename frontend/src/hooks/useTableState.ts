import { useCallback, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'

/**
 * Estado de tabela (página, tamanho, ordenação, busca e filtros) guardado na URL.
 * Assim paginação/ordenação/filtros são PRESERVADOS ao abrir um detalhe e voltar
 * (DS §10) e o link pode ser compartilhado. Gera exatamente os parâmetros do contrato v1:
 *   ?page=1&page_size=25&sort=-created_at&q=...&<filtro>=...
 */
export interface TableState {
  page: number
  pageSize: number
  /** "campo" (asc) ou "-campo" (desc). */
  sort: string | null
  q: string
  filters: Record<string, string>
}

export interface UseTableStateOptions {
  defaultPageSize?: 10 | 25 | 50 | 100
  defaultSort?: string
  /** Chaves de filtro reconhecidas (demais parâmetros da URL são ignorados). */
  filterKeys?: string[]
  /** Prefixo para mais de uma tabela na mesma página. */
  prefix?: string
}

export const PAGE_SIZES = [10, 25, 50, 100] as const

export function useTableState(opts: UseTableStateOptions = {}) {
  const { defaultPageSize = 25, defaultSort, filterKeys = [], prefix = '' } = opts
  const [sp, setSp] = useSearchParams()
  const k = useCallback((name: string) => `${prefix}${name}`, [prefix])

  const state: TableState = useMemo(() => {
    const page = Math.max(1, Number(sp.get(k('page'))) || 1)
    const sizeRaw = Number(sp.get(k('page_size')))
    const pageSize = (PAGE_SIZES as readonly number[]).includes(sizeRaw) ? sizeRaw : defaultPageSize
    const filters: Record<string, string> = {}
    for (const f of filterKeys) {
      const v = sp.get(k(f))
      if (v) filters[f] = v
    }
    return { page, pageSize, sort: sp.get(k('sort')) ?? defaultSort ?? null, q: sp.get(k('q')) ?? '', filters }
    // filterKeys como string para estabilidade
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sp, k, defaultPageSize, defaultSort, filterKeys.join('|')])

  const update = useCallback(
    (patch: Record<string, string | number | null | undefined>, resetPage = false) => {
      setSp(
        (prev) => {
          const next = new URLSearchParams(prev)
          for (const [name, value] of Object.entries(patch)) {
            if (value === null || value === undefined || value === '') next.delete(k(name))
            else next.set(k(name), String(value))
          }
          if (resetPage) next.delete(k('page'))
          return next
        },
        { replace: true },
      )
    },
    [setSp, k],
  )

  return {
    state,
    setPage: (page: number) => update({ page: page <= 1 ? null : page }),
    setPageSize: (size: number) => update({ page_size: size === defaultPageSize ? null : size }, true),
    /** Alterna asc → desc → padrão ao clicar no mesmo campo. */
    toggleSort: (field: string) => {
      const cur = state.sort
      const next = cur === field ? `-${field}` : cur === `-${field}` ? (defaultSort ?? null) : field
      update({ sort: next === defaultSort ? null : next }, true)
    },
    setQuery: (q: string) => update({ q }, true),
    setFilter: (name: string, value: string | null) => update({ [name]: value }, true),
    clearFilters: () => update(Object.fromEntries([...filterKeys, 'q'].map((f) => [f, null])), true),
    /** Parâmetros prontos para a API (contrato v1 §1). */
    apiParams: {
      page: state.page,
      page_size: state.pageSize,
      sort: state.sort ?? undefined,
      q: state.q || undefined,
      ...state.filters,
    } as Record<string, string | number | undefined>,
  }
}
