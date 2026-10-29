/**
 * RealtimeProvider — um único WebSocket por aba, aberto só com sessão autenticada.
 * Eventos → invalidação das queries afetadas (eventMap.ts); reconexão → um refetch das
 * queries ativas. Expõe status e horário da última sincronização ("Atualizado às HH:MM").
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { http } from '@/lib/http'
import { RealtimeConnection, type RealtimeStatus } from './connection'
import { invalidationsFor, type RealtimeEventType } from './eventMap'
import { RealtimeContext, wsUrl, type RealtimeHandler } from './context'

interface ProviderProps {
  enabled: boolean
  /**
   * Identidade da sessão (ex.: `me.user.id`). Quando muda — início/fim de impersonation —
   * o WS é reaberto com um NOVO ticket (escopo do novo usuário) e o cache é invalidado.
   */
  identity?: string | null
  children: ReactNode
  /** Injeção para testes. */
  WebSocketImpl?: typeof WebSocket
  random?: () => number
}

export function RealtimeProvider({ enabled, identity = null, children, WebSocketImpl, random }: ProviderProps) {
  const queryClient = useQueryClient()
  const [status, setStatus] = useState<RealtimeStatus>('idle')
  const [lastSyncAt, setLastSyncAt] = useState<Date | null>(null)
  const handlers = useRef(new Map<string, Set<RealtimeHandler>>())
  const lastIdentity = useRef<string | null>(identity)

  // Troca de identidade com a sessão ativa (impersonation): dados do usuário anterior ficam inválidos.
  useEffect(() => {
    const prev = lastIdentity.current
    lastIdentity.current = identity
    if (prev && identity && prev !== identity) {
      void queryClient.invalidateQueries({ refetchType: 'active' })
    }
  }, [identity, queryClient])

  const subscribe = useCallback((type: RealtimeEventType | '*', handler: RealtimeHandler) => {
    const map = handlers.current
    let set = map.get(type)
    if (!set) {
      set = new Set()
      map.set(type, set)
    }
    set.add(handler)
    return () => {
      map.get(type)?.delete(handler)
    }
  }, [])

  useEffect(() => {
    if (!enabled) {
      setStatus('idle')
      return
    }
    const conn = new RealtimeConnection({
      WebSocketImpl,
      random,
      getTicket: async () => {
        const res = await http.post<{ ticket: string }>('/realtime/ticket')
        return res.ticket
      },
      buildUrl: (ticket) => wsUrl('/ws/admin', ticket),
      onStatus: (s) => {
        setStatus(s)
        if (s === 'open') setLastSyncAt(new Date())
      },
      onResync: () => {
        void queryClient.invalidateQueries({ refetchType: 'active' })
        setLastSyncAt(new Date())
      },
      onEvent: (event) => {
        for (const domain of invalidationsFor(event.type)) {
          void queryClient.invalidateQueries({ queryKey: [domain], refetchType: 'active' })
        }
        setLastSyncAt(new Date())
        handlers.current.get(event.type)?.forEach((h) => h(event))
        handlers.current.get('*')?.forEach((h) => h(event))
      },
    })
    conn.start()
    return () => conn.stop()
  }, [enabled, identity, queryClient, WebSocketImpl, random])

  const value = useMemo(() => ({ status, lastSyncAt, subscribe }), [status, lastSyncAt, subscribe])
  return <RealtimeContext.Provider value={value}>{children}</RealtimeContext.Provider>
}
