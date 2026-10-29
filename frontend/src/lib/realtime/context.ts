import { createContext, useContext, useEffect, useRef } from 'react'
import type { RealtimeStatus } from './connection'
import type { RealtimeEnvelope, RealtimeEventType } from './eventMap'

export type RealtimeHandler = (event: RealtimeEnvelope) => void

export interface RealtimeContextValue {
  status: RealtimeStatus
  /** Última vez em que os dados foram confirmados como atuais (conexão aberta, resync ou evento). */
  lastSyncAt: Date | null
  subscribe: (type: RealtimeEventType | '*', handler: RealtimeHandler) => () => void
}

export const RealtimeContext = createContext<RealtimeContextValue>({
  status: 'idle',
  lastSyncAt: null,
  subscribe: () => () => undefined,
})

export function useRealtimeStatus() {
  const { status, lastSyncAt } = useContext(RealtimeContext)
  return { status, lastSyncAt, live: status === 'open' }
}

/**
 * Reage a um evento específico (ex.: atualizar uma linha com setQueryData sem refetch).
 * O handler mais recente é sempre usado; não precisa de useCallback.
 */
export function useRealtimeEvent(type: RealtimeEventType | '*', handler: RealtimeHandler) {
  const { subscribe } = useContext(RealtimeContext)
  const ref = useRef(handler)
  useEffect(() => {
    ref.current = handler
  })
  useEffect(() => subscribe(type, (e) => ref.current(e)), [subscribe, type])
}

export function wsUrl(path: string, ticket: string): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}${path}?ticket=${encodeURIComponent(ticket)}`
}
