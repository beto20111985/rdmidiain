/** Hooks utilitários das páginas de Administração/Suporte/Design (sem polling). */
import * as React from 'react'
import type { useTableState } from '@/hooks/useTableState'

/** Valor com atraso (debounce) — para busca no servidor. */
export function useDebounced<T>(value: T, ms = 300): T {
  const [v, setV] = React.useState(value)
  React.useEffect(() => {
    const t = window.setTimeout(() => setV(value), ms)
    return () => window.clearTimeout(t)
  }, [value, ms])
  return v
}

/** Object URL de um Blob, revogado ao trocar/desmontar. */
export function useObjectUrl(blob: Blob | null | undefined): string | null {
  const [url, setUrl] = React.useState<string | null>(null)
  React.useEffect(() => {
    if (!blob) {
      setUrl(null)
      return
    }
    const u = URL.createObjectURL(blob)
    setUrl(u)
    return () => URL.revokeObjectURL(u)
  }, [blob])
  return url
}

/**
 * Campo de busca com atraso (300 ms) ligado ao `q` da URL do useTableState.
 * "Limpar filtros" na tabela também limpa o campo.
 */
export function useTableSearch(table: ReturnType<typeof useTableState>) {
  const [search, setSearch] = React.useState(table.state.q)
  const debounced = useDebounced(search)
  const { setQuery } = table
  const q = table.state.q
  React.useEffect(() => {
    if (debounced !== q) setQuery(debounced)
    // Só reage à digitação (debounced); `q` muda como consequência.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debounced])
  React.useEffect(() => {
    if (q === '') setSearch('')
  }, [q])
  return [search, setSearch] as const
}
