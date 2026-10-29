/**
 * Contexto ativo (E2 #779–784): a mesma conta alterna entre "RD", "Minha unidade" e
 * "Minhas campanhas", conforme `GET /me.contexts`. O contexto é enviado em `X-RD-Context`
 * (filtro de UX — a autorização é sempre do servidor).
 * Guardado em sessionStorage (aba atual), nunca em localStorage.
 */
import { create } from 'zustand'
import { setActiveContextHeader } from '@/lib/http'
import { queryClient } from '@/lib/queryClient'
import type { AppContextInfo, ContextKind } from '@/features/auth/types'

export type RouteContext = 'rd' | 'establishment' | 'advertiser'

export const CONTEXT_KIND_TO_ROUTE: Record<ContextKind, RouteContext> = {
  PLATFORM: 'rd',
  ESTABLISHMENT: 'establishment',
  ADVERTISER: 'advertiser',
}

const STORAGE_KEY = 'rd-active-context'

function readStored(): string | null {
  try {
    return window.sessionStorage.getItem(STORAGE_KEY)
  } catch {
    return null
  }
}
function writeStored(key: string | null) {
  try {
    if (key) window.sessionStorage.setItem(STORAGE_KEY, key)
    else window.sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    /* ignora */
  }
}

interface ContextState {
  contexts: AppContextInfo[]
  activeKey: string | null
  /** Recebe os contextos de /me e escolhe o ativo (o salvo na aba, se ainda válido). */
  syncFromMe: (contexts: AppContextInfo[]) => void
  /** Troca de contexto: limpa o cache de dados (exceto /me) para não misturar escopos. */
  setActive: (key: string) => void
  reset: () => void
}

export const useContextStore = create<ContextState>((set, get) => ({
  contexts: [],
  activeKey: null,
  syncFromMe: (contexts) => {
    const current = get().activeKey ?? readStored()
    const next = contexts.find((c) => c.key === current)?.key ?? contexts[0]?.key ?? null
    setActiveContextHeader(next)
    writeStored(next)
    set({ contexts, activeKey: next })
  },
  setActive: (key) => {
    if (key === get().activeKey) return
    if (!get().contexts.some((c) => c.key === key)) return
    setActiveContextHeader(key)
    writeStored(key)
    void queryClient.cancelQueries({ predicate: (q) => q.queryKey[0] !== 'me' })
    queryClient.removeQueries({ predicate: (q) => q.queryKey[0] !== 'me' })
    set({ activeKey: key })
  },
  reset: () => {
    setActiveContextHeader(null)
    writeStored(null)
    set({ contexts: [], activeKey: null })
  },
}))

export function activeContextInfo(): AppContextInfo | null {
  const { contexts, activeKey } = useContextStore.getState()
  return contexts.find((c) => c.key === activeKey) ?? null
}

export function useActiveContext(): AppContextInfo | null {
  return useContextStore((s) => s.contexts.find((c) => c.key === s.activeKey) ?? null)
}

export function useActiveRouteContext(): RouteContext | null {
  const ctx = useActiveContext()
  return ctx ? CONTEXT_KIND_TO_ROUTE[ctx.kind] : null
}
