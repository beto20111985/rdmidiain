/**
 * Evento de tempo real → queries do TanStack que ficam desatualizadas (contrato v1 §5).
 * Cada evento invalida por PREFIXO de queryKey (ver src/lib/queryKeys.ts) e só refaz as
 * queries ATIVAS (montadas). Agentes de páginas: se a sua página precisa reagir a um evento,
 * use as raízes abaixo como primeiro elemento da queryKey; para atualização incremental
 * (setQueryData) use `useRealtimeEvent` em vez de mudar este mapa.
 */
import type { QueryDomain } from '@/lib/queryKeys'

export const REALTIME_EVENT_TYPES = [
  'screen.status',
  'screen.sync',
  'campaign.updated',
  'approval.pending',
  'payment.updated',
  'media.processing',
  'inventory.updated',
  'notification.created',
  'finance.approval_request',
  'withdrawal.updated',
  'ticket.updated',
  'designer.updated',
  // Domínio W-ORG (FE-OPS): cadastro de telas/estabelecimentos, estoque, manutenção e onboarding.
  'screen.updated',
  'establishment.updated',
  'equipment.updated',
  'maintenance.updated',
  'onboarding.updated',
  // AÇÃO 003: modo de aprovação, convite/ativação do 2º Master, recuperação (entregue a cada Master).
  'security.status_changed',
] as const

export type RealtimeEventType = (typeof REALTIME_EVENT_TYPES)[number]

export interface RealtimeEnvelope<T = Record<string, unknown>> {
  type: RealtimeEventType | string
  id: string
  ts: string
  data: T
}

export const EVENT_INVALIDATIONS: Record<RealtimeEventType, QueryDomain[]> = {
  'screen.status': ['screens', 'dashboard', 'establishments', 'maintenance'],
  'screen.sync': ['screens', 'playlists', 'schedule'],
  'campaign.updated': ['campaigns', 'approvals', 'creatives', 'dashboard', 'inventory', 'schedule'],
  'approval.pending': ['approvals', 'campaigns', 'dashboard'],
  'payment.updated': ['payments', 'invoices', 'checkout', 'finance', 'campaigns', 'dashboard'],
  'media.processing': ['media', 'creatives'],
  'inventory.updated': ['inventory', 'schedule', 'checkout'],
  'notification.created': ['notifications'],
  'finance.approval_request': ['finance', 'notifications'],
  'withdrawal.updated': ['withdrawals', 'commissions', 'finance'],
  'ticket.updated': ['support'],
  'designer.updated': ['design'],
  'screen.updated': ['screens', 'establishments', 'inventory', 'maintenance'],
  'establishment.updated': ['establishments', 'screens', 'maintenance'],
  'equipment.updated': ['equipment', 'screens'],
  'maintenance.updated': ['maintenance'],
  'onboarding.updated': ['establishments'],
  // /me (security_setup) não é uma query do TanStack: o AppShell recarrega via useRealtimeEvent.
  'security.status_changed': ['me', 'security', 'finance'],
}

export function invalidationsFor(type: string): QueryDomain[] {
  return (EVENT_INVALIDATIONS as Record<string, QueryDomain[]>)[type] ?? []
}
