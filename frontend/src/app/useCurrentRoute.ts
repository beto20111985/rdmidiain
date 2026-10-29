import { useMemo } from 'react'
import { matchRoutes, useLocation } from 'react-router-dom'
import { ROUTES, type AppRoute } from './routes'

const MATCHABLE = ROUTES.map((r) => ({ path: r.path, id: r.id }))

/** Rota de routes.ts que corresponde à URL atual (rotas estáticas vencem dinâmicas). */
export function useCurrentAppRoute(): AppRoute | null {
  const location = useLocation()
  return useMemo(() => {
    const m = matchRoutes(MATCHABLE, location)
    const id = m?.[m.length - 1]?.route.id
    return ROUTES.find((r) => r.id === id) ?? null
  }, [location])
}
