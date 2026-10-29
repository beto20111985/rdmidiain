/** Caminhos do módulo de design por contexto (RD / Minha unidade / Minhas campanhas). */
import type { RouteContext } from '@/stores/contextStore'

export const DESIGN_PATHS: Record<RouteContext, { list: string; detail: (id: string) => string; new: string }> = {
  rd: { list: '/design/briefings', detail: (id) => `/design/briefings/${id}`, new: '/design/briefings/novo' },
  establishment: { list: '/unidade/design', detail: (id) => `/unidade/design/${id}`, new: '/unidade/design/novo' },
  advertiser: { list: '/anunciante/design', detail: (id) => `/anunciante/design/${id}`, new: '/anunciante/design/novo' },
}

/** Revisões: usadas / limite (incluídas + extras liberadas). */
export function revisionLimit(r: { revisions_included: number; extra_revisions_count: number }): number {
  return r.revisions_included + r.extra_revisions_count
}
