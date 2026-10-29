/**
 * Nomes dos escopos ORGANIZATION/ESTABLISHMENT das atribuições (GET /users devolve só o id).
 * Busca cada empresa/estabelecimento uma vez (cache do TanStack); sem acesso → id curto.
 */
import { useQueries } from '@tanstack/react-query'
import { http } from '@/lib/http'

interface Scoped {
  scope_type: string
  scope_id?: string | null
}

interface Named {
  id: string
  name: string
  trade_name?: string | null
}

export function useScopeLabels(assignments: Scoped[]): (a: Scoped) => string | null {
  const orgIds = Array.from(new Set(assignments.filter((a) => a.scope_type === 'ORGANIZATION' && a.scope_id).map((a) => a.scope_id as string)))
  const estIds = Array.from(new Set(assignments.filter((a) => a.scope_type === 'ESTABLISHMENT' && a.scope_id).map((a) => a.scope_id as string)))
  const results = useQueries({
    queries: [
      ...orgIds.map((id) => ({
        queryKey: ['organizations', 'detail', id],
        queryFn: () => http.get<Named>(`/organizations/${id}`),
        staleTime: 5 * 60_000,
        retry: false,
      })),
      ...estIds.map((id) => ({
        queryKey: ['establishments', 'detail', id],
        queryFn: () => http.get<Named>(`/establishments/${id}`),
        staleTime: 5 * 60_000,
        retry: false,
      })),
    ],
  })
  const names = new Map<string, string>()
  results.forEach((r) => {
    if (r.data?.id) names.set(r.data.id, r.data.trade_name || r.data.name)
  })
  return (a) => (a.scope_id ? names.get(a.scope_id) ?? null : null)
}
