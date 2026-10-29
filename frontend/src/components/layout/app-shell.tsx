/**
 * Estrutura do painel (DS §8):
 *  < 768 px  → sem sidebar; menu em gaveta (botão no Header)
 *  768–1199  → sidebar compacta 72 px (sem hambúrguer, sem duplicar navegação)
 *  ≥ 1200    → sidebar 248 px, recolhível pelo usuário (lembrado na aba)
 * Conteúdo fluido até 1600 px; padding 32 / 24 / 16 px. Link "Pular para o conteúdo".
 */
import * as React from 'react'
import { Outlet } from 'react-router-dom'
import { Info } from 'lucide-react'
import { useIsMobile, useIsTablet } from '@/hooks/useMediaQuery'
import { useAuthStore } from '@/stores/authStore'
import { PageSkeleton } from '@/components/patterns/states'
import { PageMetaProvider } from './page-meta'
import { Sidebar } from './sidebar'
import { MobilePageActions, Topbar } from './topbar'
import { ImpersonationBanner } from './impersonation-banner'
import { LiveIndicator } from './live-indicator'
import { SecondMasterBanner } from '@/features/security/SecondMasterBanner'
import { SecondMasterModal } from '@/features/security/SecondMasterModal'
import { SecurityStatusSync } from '@/features/security/SecurityStatusSync'

const COLLAPSE_KEY = 'rd-sidebar-collapsed'

function readCollapsed() {
  try {
    return window.sessionStorage.getItem(COLLAPSE_KEY) === '1'
  } catch {
    return false
  }
}

function DemoModeBar() {
  return (
    <div
      role="note"
      className="flex items-center gap-2 border-b border-warning/60 bg-bg bg-[linear-gradient(rgb(var(--warning)/var(--badge-alpha)),rgb(var(--warning)/var(--badge-alpha)))] px-4 py-2 text-xs font-semibold text-text md:px-6"
    >
      <Info aria-hidden="true" className="size-4 shrink-0 text-warning" />
      MODO DEMONSTRAÇÃO (somente desenvolvimento) — sessão fictícia, sem backend. Nenhum dado exibido é real.
    </div>
  )
}

export function AppShell() {
  const isMobile = useIsMobile()
  const isTablet = useIsTablet()
  const [userCollapsed, setUserCollapsed] = React.useState(readCollapsed)
  const demo = useAuthStore((s) => s.demo)
  const bannerRef = React.useRef<HTMLDivElement>(null)
  const [bannerH, setBannerH] = React.useState(0)

  React.useLayoutEffect(() => {
    const el = bannerRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => setBannerH(el.getBoundingClientRect().height))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const collapsed = isTablet || (!isMobile && userCollapsed)
  const toggle = () => {
    setUserCollapsed((v) => {
      try {
        window.sessionStorage.setItem(COLLAPSE_KEY, v ? '0' : '1')
      } catch {
        /* ignora */
      }
      return !v
    })
  }

  return (
    <PageMetaProvider>
      <div style={{ '--banner-h': `${bannerH}px` } as React.CSSProperties} className="min-h-dvh">
        <a href="#conteudo" className="rd-skip-link">
          Pular para o conteúdo
        </a>
        <div ref={bannerRef} className="sticky top-0 z-banner">
          <ImpersonationBanner />
          {demo ? <DemoModeBar /> : null}
        </div>
        <div className="flex min-h-[calc(100dvh-var(--banner-h,0px))]">
          {!isMobile ? <Sidebar collapsed={collapsed} canToggle={!isTablet} onToggle={toggle} /> : null}
          <div className="flex min-w-0 flex-1 flex-col">
            <Topbar mobileNav={isMobile} />
            <main id="conteudo" tabIndex={-1} className="mx-auto w-full min-w-0 max-w-content flex-1 px-4 py-6 outline-none md:px-6 xl:px-8 xl:py-8">
              <LiveIndicator className="mb-4 2xl:hidden" />
              <SecondMasterBanner />
              <MobilePageActions />
              <React.Suspense fallback={<PageSkeleton />}>
                <Outlet />
              </React.Suspense>
            </main>
          </div>
        </div>
        {/* AÇÃO 003: tempo real do estado de segurança + aviso do 2º Master (somente na aprovação dupla) */}
        <SecurityStatusSync />
        <SecondMasterModal />
      </div>
    </PageMetaProvider>
  )
}
