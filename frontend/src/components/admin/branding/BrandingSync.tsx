/**
 * Carrega a marca pública da instância UMA vez (TanStack Query, também antes do login) e a
 * aplica: tokens de cor, favicon e estado em memória lido pelo logo e pelos títulos.
 * Erro de rede / rota ausente → marca RD padrão (nada sobrescrito). Sem polling: atualiza
 * quando a query é invalidada (ex.: ao salvar a página White-label).
 */
import * as React from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { http } from '@/lib/http'
import { BRANDING_QUERY_KEY } from './query'
import { DEFAULT_BRAND, applyBrandToDocument, resolveBrand, useBrandStore, type BrandingResponse } from './brand'

export function BrandingSync() {
  const setBrand = useBrandStore((s) => s.setBrand)
  const qc = useQueryClient()
  // Logout (`clear`) e troca de contexto (`removeQueries`) removem a query do cache: re-renderiza
  // para o observador recriá-la (1 nova leitura pública) e continuar recebendo invalidações.
  const [, rebuild] = React.useReducer((x: number) => x + 1, 0)
  React.useEffect(
    () =>
      qc.getQueryCache().subscribe((e) => {
        if (e.type === 'removed' && e.query.queryKey[0] === BRANDING_QUERY_KEY[0] && e.query.queryKey[1] === BRANDING_QUERY_KEY[1]) rebuild()
      }),
    [qc],
  )
  const q = useQuery({
    queryKey: BRANDING_QUERY_KEY,
    queryFn: () => http.get<BrandingResponse>('/instance/branding', { auth: false, retryOn401: false, sendContext: false }),
    staleTime: Infinity,
    retry: false,
    refetchOnWindowFocus: false,
  })
  React.useEffect(() => {
    if (q.isPending && !q.data) return
    const brand = q.data && !q.error ? resolveBrand(q.data, q.dataUpdatedAt) : DEFAULT_BRAND
    setBrand(brand)
    applyBrandToDocument(brand)
  }, [q.data, q.error, q.isPending, q.dataUpdatedAt, setBrand])
  return null
}
