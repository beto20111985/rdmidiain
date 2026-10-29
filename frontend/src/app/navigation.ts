/** Monta a árvore do menu a partir de routes.ts (seções → itens/grupos), já filtrada por RBAC. */
import { NAV_GROUPS, NAV_SECTIONS, visibleNavRoutes, type AppRoute, type NavGroup, type NavSection } from './routes'
import type { RouteContext } from '@/stores/contextStore'

export interface NavItemNode {
  kind: 'item'
  route: AppRoute
}
export interface NavGroupNode {
  kind: 'group'
  group: NavGroup
  items: AppRoute[]
}
export type NavNode = NavItemNode | NavGroupNode
export interface NavSectionNode {
  section: NavSection
  nodes: NavNode[]
}

export function buildNav(context: RouteContext | null, permissions: readonly string[] | undefined): NavSectionNode[] {
  const routes = visibleNavRoutes(context, permissions)
  const out: NavSectionNode[] = []
  for (const section of NAV_SECTIONS) {
    const inSection = routes.filter((r) => r.nav?.section === section.id)
    if (!inSection.length) continue
    const nodes: NavNode[] = []
    const seenGroups = new Set<string>()
    for (const r of inSection) {
      const gid = r.nav?.group
      if (!gid) {
        nodes.push({ kind: 'item', route: r })
        continue
      }
      if (seenGroups.has(gid)) continue
      seenGroups.add(gid)
      const group = NAV_GROUPS.find((g) => g.id === gid)
      const items = inSection.filter((x) => x.nav?.group === gid)
      // Grupo com um único item visível vira item simples (sem submenu vazio).
      if (!group || items.length === 1) items.forEach((route) => nodes.push({ kind: 'item', route }))
      else nodes.push({ kind: 'group', group, items })
    }
    out.push({ section, nodes })
  }
  return out
}

/** Rota de menu correspondente ao caminho atual (prefixo mais longo). */
export function activeNavRouteId(pathname: string, routes: AppRoute[]): string | null {
  let best: AppRoute | null = null
  for (const r of routes) {
    if (pathname === r.path || pathname.startsWith(`${r.path}/`)) {
      if (!best || r.path.length > best.path.length) best = r
    }
  }
  return best?.id ?? null
}
