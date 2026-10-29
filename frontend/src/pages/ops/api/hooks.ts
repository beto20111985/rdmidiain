/**
 * Hooks de dados do domínio de Operação (FE-OPS).
 * - queryKey SEMPRE começa por um QueryDomain (src/lib/queryKeys.ts) — o tempo real invalida por prefixo.
 * - Presença: domínio 'presence', atualizado por setQueryData no evento `screen.status` (sem polling e
 *   sem refetch em massa); a reconexão do WebSocket refaz as consultas ativas.
 */
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { qk } from '@/lib/queryKeys'
import { useCan } from '@/hooks/usePermissions'
import { useRealtimeEvent } from '@/lib/realtime/context'
import type { RealtimeEnvelope } from '@/lib/realtime/eventMap'
import {
  assignableUsers,
  creativesApi,
  establishmentsApi,
  plansApi,
  screensApi,
  screensPresenceBatch,
  presenceToSummary,
  taxonomyApi,
  usersApi,
} from './client'
import type { PresenceSummary, ScreenPresence } from './types'

const LOOKUP_STALE = 5 * 60 * 1000

/** Dados do evento `screen.status` (W-PLAY presence.publish_status). */
export interface ScreenStatusEvent {
  screen_id: string
  screen_code?: string | null
  device_id?: string | null
  presence?: string
  status?: string
  sync?: string | null
  player_state?: string | null
  manifest_version_applied?: number | null
  manifest_version_pending?: number | null
  last_seen_ms?: number | null
  app_version?: string | null
  download?: { done_bytes: number | null; total_bytes: number | null } | null
  error?: { kind?: string; media_id?: string; reason?: string } | null
}

/** Aplica um evento `screen.status` a um resumo de presença. */
export function applyStatusEvent(prev: PresenceSummary | undefined, ev: ScreenStatusEvent): PresenceSummary {
  return {
    ...(prev ?? { paired: true, status: 'offline' }),
    screen_id: ev.screen_id,
    paired: ev.device_id ? true : (prev?.paired ?? true),
    device_id: ev.device_id ?? prev?.device_id ?? null,
    status: ev.status ?? prev?.status ?? 'offline',
    player_state: ev.player_state ?? prev?.player_state ?? null,
    last_seen_ms: ev.last_seen_ms ?? prev?.last_seen_ms ?? null,
    app_version: ev.app_version ?? prev?.app_version ?? null,
    manifest_version_applied: ev.manifest_version_applied ?? prev?.manifest_version_applied ?? null,
    manifest_version_pending:
      ev.manifest_version_pending !== undefined ? ev.manifest_version_pending : (prev?.manifest_version_pending ?? null),
    download: ev.download !== undefined ? ev.download : (prev?.download ?? null),
  }
}

/** Presença de várias telas (lista). Atualiza a linha afetada a cada `screen.status`. */
export function useScreensPresence(ids: string[], enabled = true) {
  const qc = useQueryClient()
  const sorted = [...ids].sort()
  const key = qk.sub('presence', 'batch', sorted.join(','))
  const query = useQuery({
    queryKey: key,
    queryFn: () => screensPresenceBatch(sorted),
    enabled: enabled && sorted.length > 0,
    staleTime: 60_000,
  })
  useRealtimeEvent('screen.status', (e: RealtimeEnvelope) => {
    const ev = e.data as unknown as ScreenStatusEvent
    if (!ev?.screen_id || !sorted.includes(ev.screen_id)) return
    qc.setQueryData<Record<string, PresenceSummary>>(key, (old) =>
      old ? { ...old, [ev.screen_id]: applyStatusEvent(old[ev.screen_id], ev) } : old,
    )
  })
  return query
}

/** Presença detalhada de uma tela (detalhe). */
export function useScreenPresence(screenId: string | undefined) {
  const qc = useQueryClient()
  const key = qk.sub('presence', 'screen', screenId ?? '')
  const query = useQuery({
    queryKey: key,
    queryFn: () => screensApi.presence(screenId!),
    enabled: !!screenId,
    staleTime: 60_000,
  })
  useRealtimeEvent('screen.status', (e: RealtimeEnvelope) => {
    const ev = e.data as unknown as ScreenStatusEvent
    if (!screenId || ev?.screen_id !== screenId) return
    qc.setQueryData<ScreenPresence>(key, (old) => {
      if (!old) return old
      const s = applyStatusEvent(presenceToSummary(old), ev)
      return {
        ...old,
        paired: s.paired,
        live: {
          ...old.live,
          status: s.status,
          player_state: s.player_state,
          last_seen_ms: s.last_seen_ms,
          app_version: s.app_version,
          manifest_version_applied: s.manifest_version_applied,
          manifest_version_pending: s.manifest_version_pending,
          download: s.download,
        },
      }
    })
    // Novo dispositivo pareado ou revogado: recarrega o detalhe do dispositivo.
    if (ev.device_id && ev.device_id !== qc.getQueryData<ScreenPresence>(key)?.device?.id) {
      void qc.invalidateQueries({ queryKey: key })
    }
  })
  return query
}

// --------------------------------------------------------------------------- Lookups (nome por id)
export function useEstablishmentLookup(id: string | null | undefined) {
  const can = useCan()
  return useQuery({
    queryKey: qk.detail('establishments', id ?? ''),
    queryFn: () => establishmentsApi.get(id!),
    enabled: !!id && can('establishments.view'),
    staleTime: LOOKUP_STALE,
    retry: false,
  })
}

export function useScreenLookup(id: string | null | undefined) {
  const can = useCan()
  return useQuery({
    queryKey: qk.detail('screens', id ?? ''),
    queryFn: () => screensApi.get(id!),
    enabled: !!id && can('screens.view'),
    staleTime: LOOKUP_STALE,
    retry: false,
  })
}

export function useCreativeLookup(id: string | null | undefined) {
  const can = useCan()
  return useQuery({
    queryKey: qk.detail('creatives', id ?? ''),
    queryFn: () => creativesApi.get(id!),
    enabled: !!id && can('creatives.view'),
    staleTime: LOOKUP_STALE,
    retry: false,
  })
}

export function useUserLookup(id: string | null | undefined) {
  const can = useCan()
  return useQuery({
    queryKey: qk.detail('users', id ?? ''),
    queryFn: () => usersApi.get(id!),
    enabled: !!id && can('users.view'),
    staleTime: LOOKUP_STALE,
    retry: false,
  })
}

// --------------------------------------------------------------------------- Catálogos
export function useTaxonomyNodes() {
  return useQuery({ queryKey: qk.sub('taxonomy', 'nodes'), queryFn: taxonomyApi.nodes, staleTime: LOOKUP_STALE })
}

export function useSegments() {
  return useQuery({ queryKey: qk.sub('taxonomy', 'segments'), queryFn: taxonomyApi.segments, staleTime: LOOKUP_STALE })
}

export function usePartnerPlans(includeInactive = false) {
  return useQuery({
    queryKey: qk.sub('establishments', 'partner-plans', includeInactive ? 'all' : 'active'),
    queryFn: () => plansApi.list(includeInactive),
    staleTime: LOOKUP_STALE,
  })
}

/**
 * Técnicos atribuíveis (`GET /users/assignable`): exige que o ator tenha a própria `permission`;
 * com `establishmentId`, só quem atende aquele estabelecimento.
 */
export function useTechnicians(
  permission: 'maintenance.manage' | 'equipment.manage',
  establishmentId?: string | null,
  enabled = true,
) {
  const can = useCan()
  return useQuery({
    queryKey: qk.sub('users', 'assignable', permission, establishmentId ?? 'all'),
    queryFn: () => assignableUsers(permission, establishmentId),
    enabled: enabled && can(permission),
    staleTime: LOOKUP_STALE,
    retry: false,
  })
}
