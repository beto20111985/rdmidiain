/**
 * Guardas de acesso (A3 §13.3–§13.4):
 * - <RequireAuth>: baseado na SESSÃO REAL (refresh no boot + GET /me), não em flag local.
 * - <RequirePermission>: mostra o estado "Sem permissão" do DS quando falta a permissão.
 * - <Can>: esconde ações sem permissão (Aprovar, Sacar, Publicar...).
 * O frontend só melhora a UX; o backend continua sendo a autoridade (403/404).
 */
import * as React from 'react'
import { Link, Navigate, useLocation } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import { ErrorState, NoPermission } from '@/components/patterns/states'
import { useAuthStore } from '@/stores/authStore'
import { useCan } from '@/hooks/usePermissions'
import type { Permission } from '@/lib/permissions'
import { BootScreen } from './boot-screen'

export function RequireAuth({ children }: { children: React.ReactNode }) {
  const status = useAuthStore((s) => s.status)
  const bootstrap = useAuthStore((s) => s.bootstrap)
  const location = useLocation()
  const next = `${location.pathname}${location.search}`

  switch (status) {
    case 'booting':
      return <BootScreen />
    case 'unavailable':
      return (
        <main className="flex min-h-dvh items-center justify-center p-4">
          <h1 className="sr-only">RD Mídia Indoor</h1>
          <ErrorState
            title="Não foi possível verificar sua sessão"
            description="O servidor não respondeu. Verifique sua conexão e tente novamente."
            onRetry={() => void bootstrap()}
          />
        </main>
      )
    case 'mfa_required':
      return <Navigate to="/login/verificacao" replace />
    case 'setup_required':
      // Primeiro acesso: troca de senha, TOTP e senha financeira (assistente em /primeiro-acesso).
      return <Navigate to="/primeiro-acesso" replace />
    case 'anonymous':
      return <Navigate to={`/login${next && next !== '/' ? `?next=${encodeURIComponent(next)}` : ''}`} replace />
    default:
      return <>{children}</>
  }
}

export interface RequirePermissionProps {
  permission: Permission | null
  children: React.ReactNode
  /** Conteúdo alternativo (padrão: estado NoPermission com orientação). */
  fallback?: React.ReactNode
}

export function RequirePermission({ permission, children, fallback }: RequirePermissionProps) {
  const can = useCan()
  if (permission === null || can(permission)) return <>{children}</>
  return (
    <>
      {fallback ?? (
        <NoPermission
          permission={permission}
          action={
            <Button asChild variant="secondary">
              <Link to="/">Ir para o início</Link>
            </Button>
          }
        />
      )}
    </>
  )
}

/** Renderiza os filhos só com a permissão. Ex.: <Can permission="campaigns.approve"><Button>Aprovar</Button></Can> */
export function Can({ permission, children, otherwise = null }: { permission: Permission; children: React.ReactNode; otherwise?: React.ReactNode }) {
  const can = useCan()
  return <>{can(permission) ? children : otherwise}</>
}
