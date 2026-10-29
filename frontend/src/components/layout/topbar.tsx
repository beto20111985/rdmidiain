/**
 * Header (DS §9): ≥ 72 px, título da página (ÚNICO <h1>), contexto da conta/local e ação
 * principal; à direita indicador de tempo real, notificações e conta. Breadcrumb só em
 * páginas internas. No mobile (< 768 px) abre a gaveta de navegação (Esc fecha e devolve
 * o foco ao botão — Radix Dialog).
 */
import * as React from 'react'
import { Link, useLocation } from 'react-router-dom'
import { ChevronRight, Menu } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Sheet, SheetContent, SheetDescription, SheetTitle, SheetTrigger } from '@/components/ui/sheet'
import { useCurrentAppRoute } from '@/app/useCurrentRoute'
import { usePageMetaValue } from './page-meta'
import { ContextSwitcher } from './context-switcher'
import { NotificationBell } from './notification-bell'
import { UserMenu } from './user-menu'
import { LiveIndicator } from './live-indicator'
import { SidebarNav } from './sidebar'
import { LogoHorizontal } from './logo'
import { pageTitle, useBrandName } from '@/components/admin/branding/brand'

export function Topbar({ mobileNav }: { mobileNav: boolean }) {
  const route = useCurrentAppRoute()
  const meta = usePageMetaValue()
  const brandName = useBrandName()
  const title = meta.title ?? route?.title ?? brandName
  const [drawerOpen, setDrawerOpen] = React.useState(false)
  const h1Ref = React.useRef<HTMLHeadingElement>(null)
  const { pathname } = useLocation()
  const firstRender = React.useRef(true)

  React.useEffect(() => {
    document.title = pageTitle(title, brandName)
  }, [title, brandName])

  // Ao navegar, o foco vai para o título da nova página (anunciado por leitores de tela).
  React.useEffect(() => {
    if (firstRender.current) {
      firstRender.current = false
      return
    }
    h1Ref.current?.focus({ preventScroll: true })
  }, [pathname])

  return (
    <header className="sticky top-[var(--banner-h,0px)] z-header flex min-h-header items-center gap-3 border-b border-border bg-bg px-4 py-3 md:px-6 xl:px-8">
      {mobileNav ? (
        <Sheet open={drawerOpen} onOpenChange={setDrawerOpen}>
          <SheetTrigger asChild>
            <Button variant="ghost" size="icon" aria-label="Abrir menu de navegação">
              <Menu aria-hidden="true" />
            </Button>
          </SheetTrigger>
          <SheetContent side="left" className="gap-0 bg-sidebar p-0" closeLabel="Fechar menu de navegação">
            <SheetTitle className="sr-only">Menu de navegação</SheetTitle>
            <SheetDescription className="sr-only">Páginas disponíveis para o seu perfil.</SheetDescription>
            <div className="flex min-h-header items-center border-b border-border px-5">
              <LogoHorizontal />
            </div>
            <SidebarNav onNavigate={() => setDrawerOpen(false)} />
          </SheetContent>
        </Sheet>
      ) : null}

      <div className="flex min-w-0 flex-1 flex-col justify-center">
        {meta.breadcrumbs?.length ? (
          <nav aria-label="Trilha de navegação" className="mb-0.5">
            <ol className="m-0 flex list-none flex-wrap items-center gap-1 p-0 text-xs text-text-muted">
              {meta.breadcrumbs.map((b, i) => (
                <li key={`${b.label}-${i}`} className="flex items-center gap-1">
                  {b.to ? (
                    <Link to={b.to} className="rounded text-text-muted underline-offset-4 hover:text-text hover:underline">
                      {b.label}
                    </Link>
                  ) : (
                    <span aria-current="page">{b.label}</span>
                  )}
                  {i < meta.breadcrumbs!.length - 1 ? <ChevronRight aria-hidden="true" className="size-3.5" /> : null}
                </li>
              ))}
            </ol>
          </nav>
        ) : null}
        <h1 ref={h1Ref} tabIndex={-1} className="m-0 line-clamp-2 break-words text-title-page-sm text-text outline-none md:truncate md:text-title-page">
          {title}
        </h1>
        <div className="flex min-w-0 flex-wrap items-center gap-x-3">
          <ContextSwitcher />
          {meta.description ? <span className="hidden text-xs text-text-muted md:inline">{meta.description}</span> : null}
        </div>
      </div>

      {meta.actions ? <div className="hidden shrink-0 items-center gap-2 md:flex">{meta.actions}</div> : null}
      <LiveIndicator className="hidden 2xl:flex" />
      <div className="flex shrink-0 items-center gap-1">
        <NotificationBell />
        <UserMenu />
      </div>
    </header>
  )
}

/** Ação principal no mobile: logo abaixo do Header, no topo do conteúdo. */
export function MobilePageActions() {
  const meta = usePageMetaValue()
  if (!meta.actions) return null
  return <div className="mb-4 flex flex-wrap gap-2 md:hidden [&>*]:flex-1">{meta.actions}</div>
}
