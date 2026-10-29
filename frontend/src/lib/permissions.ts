/**
 * Catálogo de permissões — espelho do contrato v1 §3.1 (`recurso.ação`).
 * O backend é a autoridade (backend/app/core/permissions.py); o frontend só usa
 * estes códigos para esconder menus/ações. Teste: src/app/routes.test.ts garante que
 * toda rota/menu referencia um código existente aqui.
 */
export const PERMISSIONS = [
  'dashboard.view',
  'organizations.view',
  'organizations.manage',
  'users.view',
  'users.manage',
  'users.impersonate',
  'roles.manage',
  'establishments.view',
  'establishments.manage',
  'screens.view',
  'screens.manage',
  'screens.pair',
  'screens.command',
  'media.view',
  'media.upload',
  'media.publish',
  'media.delete',
  'playlists.view',
  'playlists.reorder',
  'playlists.manage',
  'campaigns.view',
  'campaigns.manage',
  'campaigns.approve',
  'campaigns.kill',
  'creatives.view',
  'creatives.manage',
  'taxonomy.manage',
  'conflicts.override',
  'inventory.view',
  'inventory.reserve',
  'pricing.view',
  'pricing.manage',
  'checkout.create',
  'payments.view',
  'finance.view',
  'finance.manage',
  'finance.approve',
  'finance.reconcile',
  'commissions.view',
  'commissions.adjust',
  'withdrawals.view',
  'withdrawals.request',
  'pix_keys.manage',
  'crm.view',
  'crm.manage',
  'crm.discount_approve',
  'design.view',
  'design.work',
  'design.request',
  'design.approve',
  'templates.manage',
  'support.view',
  'support.create',
  'support.manage',
  'equipment.view',
  'equipment.manage',
  'maintenance.manage',
  'reports.view',
  'reports.export',
  'reports.financial',
  'notifications.manage',
  'settings.view',
  'settings.manage',
  'audit.view',
  'audit.export',
  'control_plane.view',
  'control_plane.manage',
  'whitelabel.manage',
  'lgpd.manage',
] as const

export type Permission = (typeof PERMISSIONS)[number]

const PERMISSION_SET: ReadonlySet<string> = new Set(PERMISSIONS)

export function isKnownPermission(code: string): code is Permission {
  return PERMISSION_SET.has(code)
}

/** Verifica uma permissão numa lista vinda de GET /me. */
export function hasPermission(granted: readonly string[] | undefined, perm: Permission): boolean {
  return !!granted && granted.includes(perm)
}

export function hasAny(granted: readonly string[] | undefined, perms: readonly Permission[]): boolean {
  return perms.some((p) => hasPermission(granted, p))
}
