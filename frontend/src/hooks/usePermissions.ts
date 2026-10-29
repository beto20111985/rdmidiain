import { useCallback } from 'react'
import { useAuthStore } from '@/stores/authStore'
import { hasPermission, type Permission } from '@/lib/permissions'

/**
 * `can(perm)` do usuário atual (permissões de GET /me).
 * Uso: const can = useCan(); {can('campaigns.approve') && <Button>Aprovar</Button>}
 * Lembre: esconder no frontend é UX; o backend sempre revalida (403/404).
 */
export function useCan(): (perm: Permission) => boolean {
  const permissions = useAuthStore((s) => s.me?.permissions)
  return useCallback((perm: Permission) => hasPermission(permissions, perm), [permissions])
}

export function usePermissions(): readonly string[] {
  return useAuthStore((s) => s.me?.permissions) ?? EMPTY
}

const EMPTY: readonly string[] = []
