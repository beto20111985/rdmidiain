/**
 * Provedores globais: TanStack Query, sessão (boot por refresh + expiração), sincronização
 * do tema com o perfil, tempo real (WS só com sessão autenticada), tooltips e toasts.
 */
import * as React from 'react'
import { QueryClientProvider } from '@tanstack/react-query'
import { queryClient } from '@/lib/queryClient'
import { onRemoteLogout, onSessionExpired, onTokenRefreshed } from '@/lib/http'
import { RealtimeProvider } from '@/lib/realtime/RealtimeProvider'
import { useAuthStore } from '@/stores/authStore'
import { setThemeRemoteSync } from '@/stores/themeStore'
import { authApi } from '@/features/auth/api'
import { TooltipProvider } from '@/components/ui/tooltip'
import { Toaster } from '@/components/ui/sonner'
import { DEV_DEMO_KEY } from '@/dev/demoKey'
import { BrandingSync } from '@/components/admin/branding/BrandingSync'

function safeSessionGet(key: string): string | null {
  try {
    return window.sessionStorage.getItem(key)
  } catch {
    return null
  }
}

function SessionBridge({ children }: { children: React.ReactNode }) {
  const status = useAuthStore((s) => s.status)
  const demo = useAuthStore((s) => s.demo)
  // Identidade efetiva: muda ao iniciar/encerrar impersonation → WS reaberto com novo ticket.
  const identity = useAuthStore((s) => s.me?.user.id ?? null)

  // Boot: tenta renovar a sessão pelo cookie HttpOnly (nada de token salvo no navegador).
  React.useEffect(() => {
    const offExpired = onSessionExpired(() => useAuthStore.getState().expireSession())
    const offRemote = onRemoteLogout(() => useAuthStore.getState().remoteLogout())
    const offRefreshed = onTokenRefreshed(() => useAuthStore.getState().onTokenRefreshed())
    const unsubscribe = () => {
      offExpired()
      offRemote()
      offRefreshed()
    }
    // Somente desenvolvimento: mantém o perfil de DEMONSTRAÇÃO escolhido ao recarregar a aba.
    const demoId = import.meta.env.DEV ? safeSessionGet(DEV_DEMO_KEY) : null
    if (import.meta.env.DEV && demoId) {
      void import('@/dev/demoProfiles').then(({ DEMO_PROFILES }) => {
        const profile = DEMO_PROFILES.find((p) => p.id === demoId)
        if (profile) useAuthStore.getState().enterDemo(profile.me)
        else void useAuthStore.getState().bootstrap()
      })
    } else {
      void useAuthStore.getState().bootstrap()
    }
    return unsubscribe
  }, [])

  // Preferência de tema → perfil do usuário (PATCH /me). Falha (rota ausente) mantém só o local.
  React.useEffect(() => {
    if (status !== 'authenticated' || demo) {
      setThemeRemoteSync(null)
      return
    }
    setThemeRemoteSync((theme) => {
      authApi.updateTheme(theme).catch(() => undefined)
    })
    return () => setThemeRemoteSync(null)
  }, [status, demo])

  return (
    <RealtimeProvider enabled={status === 'authenticated' && !demo} identity={identity}>
      {children}
    </RealtimeProvider>
  )
}

export function AppProviders({ children }: { children: React.ReactNode }) {
  return (
    <QueryClientProvider client={queryClient}>
      {/* Marca da instância (white-label): pública, carregada antes do login. */}
      <BrandingSync />
      <TooltipProvider delayDuration={300}>
        <SessionBridge>{children}</SessionBridge>
        <Toaster />
      </TooltipProvider>
    </QueryClientProvider>
  )
}
