import * as React from 'react'
import { useActiveContext, useActiveRouteContext } from '@/stores/contextStore'

/**
 * Área atual das páginas de campanha: equipe RD (vê todas as campanhas, nomes de
 * estabelecimentos para segmentação/conflitos) ou anunciante/agência (somente as suas;
 * NUNCA nomes/endereços de estabelecimentos — só região, contagem e código de tela).
 */
export function useCampaignArea() {
  const routeCtx = useActiveRouteContext()
  const ctx = useActiveContext()
  const isStaff = routeCtx === 'rd'
  return {
    isStaff,
    routeContext: routeCtx,
    /** Organização do contexto ativo (anunciante); RD escolhe a organização explicitamente. */
    contextOrgId: !isStaff ? (ctx?.org_id ?? null) : null,
    campaignsBase: isStaff ? '/campanhas' : '/anunciante/campanhas',
    newCampaignPath: isStaff ? '/campanhas/nova' : '/anunciante/campanhas/nova',
    cartPath: '/anunciante/carrinho',
    checkoutPath: (orderId: string) => `/anunciante/checkout/${orderId}`,
    proofsPath: isStaff ? '/relatorios' : '/anunciante/comprovantes',
    designPath: isStaff ? '/design/briefings' : '/anunciante/design',
  }
}

/**
 * Relógio para contagens regressivas (somente exibição — não busca dados).
 * Usa `setTimeout` encadeado; o prazo vem sempre do servidor (`expires_at`).
 */
export function useNow(stepMs = 1000, enabled = true): number {
  const [now, setNow] = React.useState(() => Date.now())
  React.useEffect(() => {
    if (!enabled) return
    let t: ReturnType<typeof setTimeout>
    const tick = () => {
      setNow(Date.now())
      t = setTimeout(tick, stepMs)
    }
    t = setTimeout(tick, stepMs)
    return () => clearTimeout(t)
  }, [stepMs, enabled])
  return now
}

export function useDebounced<T>(value: T, ms = 400): T {
  const [v, setV] = React.useState(value)
  React.useEffect(() => {
    const t = setTimeout(() => setV(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return v
}

/** "1 h 59 min 03 s" a partir de milissegundos restantes. */
export function formatRemaining(ms: number): string {
  if (ms <= 0) return '0 s'
  const s = Math.floor(ms / 1000)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const r = s % 60
  if (h > 0) return `${h} h ${String(m).padStart(2, '0')} min ${String(r).padStart(2, '0')} s`
  if (m > 0) return `${m} min ${String(r).padStart(2, '0')} s`
  return `${r} s`
}
