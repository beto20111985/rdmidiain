/**
 * Convenção de queryKeys do TanStack Query.
 *
 *   [domínio]                         → raiz (o tempo real invalida por prefixo)
 *   [domínio, 'list', params]         → listas paginadas/filtradas
 *   [domínio, 'detail', id]           → um registro
 *   [domínio, <sub>, ...]             → sub-recursos (ex.: ['screens', 'detail', id, 'history'])
 *
 * O primeiro elemento é SEMPRE um `QueryDomain` — é ele que o mapa de eventos de tempo real
 * (src/lib/realtime/eventMap.ts) usa para invalidar. Nunca use string solta fora desta lista.
 */
export const QUERY_DOMAINS = [
  'me',
  'sessions',
  'dashboard',
  'organizations',
  'establishments',
  'screens',
  'pairing',
  // Presença por tela (FE-OPS): atualizada por setQueryData no evento `screen.status` — fora de
  // EVENT_INVALIDATIONS de propósito (evita refazer N consultas por evento); a reconexão refaz tudo.
  'presence',
  'media',
  'playlists',
  'schedule',
  'inventory',
  'campaigns',
  'approvals',
  'creatives',
  'taxonomy',
  'pricing',
  'checkout',
  'payments',
  'invoices',
  'finance',
  'commissions',
  'withdrawals',
  'pix-keys',
  'crm',
  'design',
  'support',
  'equipment',
  'maintenance',
  'reports',
  'notifications',
  'users',
  'roles',
  'audit',
  'settings',
  'control-plane',
  // Segurança da instância (AÇÃO 003): modo de aprovação, Masters e convite do 2º Master.
  'security',
] as const

export type QueryDomain = (typeof QUERY_DOMAINS)[number]

export const qk = {
  root: (domain: QueryDomain) => [domain] as const,
  list: <P extends object | undefined>(domain: QueryDomain, params?: P) =>
    [domain, 'list', params ?? {}] as const,
  detail: (domain: QueryDomain, id: string) => [domain, 'detail', id] as const,
  sub: (domain: QueryDomain, ...parts: Array<string | number | object>) => [domain, ...parts] as const,
  // Chaves fixas usadas pela fundação
  me: ['me'] as const,
  sessions: ['sessions'] as const,
  notificationsUnread: ['notifications', 'unread-count'] as const,
}
