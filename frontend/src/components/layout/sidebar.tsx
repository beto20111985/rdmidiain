/**
 * Sidebar (DS §8–§9): 248 px aberta / 72 px recolhida; logo no topo (padding 20 px);
 * itens ≥ 44 px, ícone outline 20 px, texto 14 px, intervalo 12 px; ativo com gradiente
 * azul profundo → ação + marcador lateral + aria-current="page"; hover em superfície elevada.
 * Itens e grupos vêm de routes.ts já filtrados por permissão e contexto.
 * Recolhida: só ícones, nome em tooltip; grupos abrem menu com os subitens.
 */
import * as React from 'react'
import { Link, useLocation } from 'react-router-dom'
import { ChevronDown, ChevronsLeft, ChevronsRight } from 'lucide-react'
import { cn } from '@/lib/utils'
import { buildNav, activeNavRouteId, type NavSectionNode } from '@/app/navigation'
import { visibleNavRoutes, type AppRoute } from '@/app/routes'
import { useActiveRouteContext } from '@/stores/contextStore'
import { usePermissions } from '@/hooks/usePermissions'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { LogoHorizontal, LogoMark } from './logo'
import { useBrandName } from '@/components/admin/branding/brand'

const itemBase =
  'group relative flex min-h-11 w-full items-center gap-3 rounded-md px-3 text-sm font-medium text-text-muted transition-colors duration-fast ease-rd hover:bg-surface-hover hover:text-text [&_svg]:size-5 [&_svg]:shrink-0'
const itemActive =
  'bg-gradient-to-r from-[rgb(var(--nav-active-from))] to-[rgb(var(--nav-active-to))] font-semibold text-[rgb(var(--nav-active-text))] hover:bg-transparent hover:text-[rgb(var(--nav-active-text))]'

function ActiveMarker() {
  return <span aria-hidden="true" className="absolute left-0 top-1/2 h-5 w-1 -translate-y-1/2 rounded-r-full bg-[rgb(var(--nav-marker))]" />
}

function NavItemLink({
  route,
  active,
  collapsed,
  onNavigate,
  nested,
}: {
  route: AppRoute
  active: boolean
  collapsed: boolean
  onNavigate?: () => void
  nested?: boolean
}) {
  const Icon = route.nav!.icon
  const link = (
    <Link
      to={route.path}
      aria-current={active ? 'page' : undefined}
      onClick={onNavigate}
      className={cn(itemBase, active && itemActive, collapsed && 'justify-center px-0', nested && !collapsed && 'pl-11 [&_svg]:hidden')}
    >
      {active ? <ActiveMarker /> : null}
      <Icon aria-hidden="true" />
      {collapsed ? <span className="sr-only">{route.nav!.label}</span> : <span className="truncate">{route.nav!.label}</span>}
    </Link>
  )
  if (!collapsed) return link
  return (
    <Tooltip>
      <TooltipTrigger asChild>{link}</TooltipTrigger>
      <TooltipContent side="right">{route.nav!.label}</TooltipContent>
    </Tooltip>
  )
}

function NavSections({
  sections,
  activeId,
  collapsed,
  onNavigate,
}: {
  sections: NavSectionNode[]
  activeId: string | null
  collapsed: boolean
  onNavigate?: () => void
}) {
  const [open, setOpen] = React.useState<Record<string, boolean>>({})
  return (
    <>
      {sections.map(({ section, nodes }, si) => {
        const headingId = `nav-sec-${section.id}`
        return (
          <div key={section.id} className={cn('flex flex-col gap-1', si > 0 && 'mt-4')}>
            {collapsed ? (
              si > 0 ? <hr aria-hidden="true" className="mx-3 mb-2 border-border" /> : null
            ) : (
              <p id={headingId} className="m-0 px-3 pb-1 text-xs font-semibold text-text-muted">
                {section.label}
              </p>
            )}
            <ul className="m-0 flex list-none flex-col gap-1 p-0" aria-labelledby={collapsed ? undefined : headingId} aria-label={collapsed ? section.label : undefined}>
              {nodes.map((node) => {
                if (node.kind === 'item') {
                  return (
                    <li key={node.route.id}>
                      <NavItemLink route={node.route} active={node.route.id === activeId} collapsed={collapsed} onNavigate={onNavigate} />
                    </li>
                  )
                }
                const { group, items } = node
                const containsActive = items.some((i) => i.id === activeId)
                const Icon = group.icon
                if (collapsed) {
                  return (
                    <li key={group.id}>
                      <DropdownMenu>
                        <Tooltip>
                          <TooltipTrigger asChild>
                            <DropdownMenuTrigger className={cn(itemBase, 'justify-center px-0', containsActive && itemActive)}>
                              {containsActive ? <ActiveMarker /> : null}
                              <Icon aria-hidden="true" />
                              <span className="sr-only">{group.label}</span>
                            </DropdownMenuTrigger>
                          </TooltipTrigger>
                          <TooltipContent side="right">{group.label}</TooltipContent>
                        </Tooltip>
                        <DropdownMenuContent side="right" align="start">
                          <DropdownMenuLabel>{group.label}</DropdownMenuLabel>
                          <DropdownMenuGroup>
                            {items.map((r) => {
                              const ItemIcon = r.nav!.icon
                              return (
                                <DropdownMenuItem key={r.id} asChild>
                                  <Link to={r.path} aria-current={r.id === activeId ? 'page' : undefined} onClick={onNavigate}>
                                    <ItemIcon aria-hidden="true" />
                                    {r.nav!.label}
                                  </Link>
                                </DropdownMenuItem>
                              )
                            })}
                          </DropdownMenuGroup>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </li>
                  )
                }
                const expanded = open[group.id] ?? containsActive
                const panelId = `nav-grp-${group.id}`
                return (
                  <li key={group.id}>
                    <button
                      type="button"
                      aria-expanded={expanded}
                      aria-controls={panelId}
                      onClick={() => setOpen((o) => ({ ...o, [group.id]: !expanded }))}
                      className={cn(itemBase, containsActive && 'text-text')}
                    >
                      <Icon aria-hidden="true" />
                      <span className="flex-1 truncate text-left">{group.label}</span>
                      <ChevronDown aria-hidden="true" className={cn('!size-4 transition-transform duration-fast ease-rd', expanded && 'rotate-180')} />
                    </button>
                    <ul id={panelId} hidden={!expanded} className={cn('m-0 mt-1 list-none flex-col gap-1 p-0', expanded ? 'flex' : 'hidden')}>
                      {items.map((r) => (
                        <li key={r.id}>
                          <NavItemLink route={r} active={r.id === activeId} collapsed={false} onNavigate={onNavigate} nested />
                        </li>
                      ))}
                    </ul>
                  </li>
                )
              })}
            </ul>
          </div>
        )
      })}
    </>
  )
}

/** Conteúdo de navegação (usado pela sidebar e pela gaveta mobile). */
export function SidebarNav({ collapsed = false, onNavigate }: { collapsed?: boolean; onNavigate?: () => void }) {
  const ctx = useActiveRouteContext()
  const permissions = usePermissions()
  const { pathname } = useLocation()
  const sections = React.useMemo(() => buildNav(ctx, permissions), [ctx, permissions])
  const activeId = React.useMemo(() => activeNavRouteId(pathname, visibleNavRoutes(ctx, permissions)), [pathname, ctx, permissions])
  return (
    <nav aria-label="Menu principal" className="flex min-h-0 flex-1 flex-col overflow-y-auto px-3 py-4">
      <NavSections sections={sections} activeId={activeId} collapsed={collapsed} onNavigate={onNavigate} />
    </nav>
  )
}

export function Sidebar({
  collapsed,
  canToggle,
  onToggle,
  footer,
}: {
  collapsed: boolean
  canToggle: boolean
  onToggle: () => void
  footer?: React.ReactNode
}) {
  const brandName = useBrandName()
  return (
    <aside
      aria-label="Navegação"
      className={cn(
        'sticky top-[var(--banner-h,0px)] flex h-[calc(100dvh-var(--banner-h,0px))] shrink-0 flex-col border-r border-border bg-sidebar transition-[width] duration-normal ease-rd',
        collapsed ? 'w-sidebar-collapsed' : 'w-sidebar',
      )}
    >
      <div className={cn('flex min-h-header items-center border-b border-border', collapsed ? 'justify-center px-2' : 'px-5')}>
        <Link to="/" aria-label={`${brandName} — início`} className="inline-flex min-h-11 items-center rounded-lg">
          {collapsed ? <LogoMark title="" /> : <LogoHorizontal />}
        </Link>
      </div>
      <SidebarNav collapsed={collapsed} />
      {footer ? <div className={cn('border-t border-border p-3', collapsed && 'px-2')}>{footer}</div> : null}
      {canToggle ? (
        <div className={cn('border-t border-border p-3', collapsed && 'flex justify-center px-2')}>
          <button
            type="button"
            onClick={onToggle}
            aria-label={collapsed ? 'Expandir menu' : 'Recolher menu'}
            className={cn(itemBase, collapsed && 'justify-center px-0')}
          >
            {collapsed ? <ChevronsRight aria-hidden="true" /> : <ChevronsLeft aria-hidden="true" />}
            {collapsed ? null : <span>Recolher menu</span>}
          </button>
        </div>
      ) : null}
    </aside>
  )
}
