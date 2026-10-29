/**
 * Ganchos compartilhados das páginas de Financeiro/CRM/Relatórios/Dashboards.
 */
import { useCallback, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { http, type Paginated } from '@/lib/http'
import { qk } from '@/lib/queryKeys'
import { useCan } from '@/hooks/usePermissions'
import { useAuthStore } from '@/stores/authStore'
import { useActiveContext } from '@/stores/contextStore'
import { useRealtimeStatus } from '@/lib/realtime/context'

interface UserLite {
  id: string
  name: string
  is_admin_master?: boolean
}

/**
 * Nome de usuário a partir do id. "Você" para o próprio usuário. Com `users.view`, consulta
 * o diretório (uma página de até 100 usuários); sem a permissão, mostra um identificador curto
 * (nunca inventa nome).
 */
export function useUserNames(): (id: string | null | undefined) => string {
  const can = useCan()
  const meId = useAuthStore((s) => s.me?.user.id)
  const allowed = can('users.view')
  const q = useQuery({
    queryKey: qk.list('users', { page_size: 100, directory: true }),
    queryFn: () => http.get<Paginated<UserLite>>('/users', { query: { page_size: 100 } }),
    enabled: allowed,
    staleTime: 10 * 60 * 1000,
  })
  const byId = useMemo(() => {
    const m = new Map<string, string>()
    for (const u of q.data?.items ?? []) m.set(u.id, u.name)
    return m
  }, [q.data])
  return useCallback(
    (id: string | null | undefined) => {
      if (!id) return '—'
      if (id === meId) return 'Você'
      return byId.get(id) ?? `Usuário ${id.slice(0, 8)}`
    },
    [byId, meId],
  )
}

/** org_id do contexto ativo ("Minha unidade" / "Minhas campanhas"); null no contexto RD. */
export function useContextOrgId(): string | null {
  const ctx = useActiveContext()
  return ctx?.org_id ?? null
}

export interface EstablishmentOption {
  id: string
  org_id: string
  name: string
  city?: string | null
  state?: string | null
  timezone?: string | null
}

/** Estabelecimentos da organização (escopo aplicado pelo servidor). */
export function useOrgEstablishments(orgId: string | null | undefined) {
  return useQuery({
    queryKey: qk.list('establishments', { org_id: orgId, page_size: 100 }),
    queryFn: () => http.get<Paginated<EstablishmentOption>>('/establishments', { query: { org_id: orgId, page_size: 100 } }),
    enabled: !!orgId,
  })
}

/** Dados podem estar desatualizados quando o tempo real caiu (sem polling: avisamos e oferecemos atualizar). */
export function useRealtimeDegraded(): { degraded: boolean; reason: string } {
  const { status } = useRealtimeStatus()
  if (status === 'reconnecting' || status === 'closed') return { degraded: true, reason: 'conexão de tempo real perdida; reconectando.' }
  if (status === 'unavailable') return { degraded: true, reason: 'tempo real indisponível; use "Atualizar agora".' }
  return { degraded: false, reason: '' }
}

/**
 * Estabelecimento selecionado (contexto "Minha unidade"): `?estabelecimento=<id>` na URL;
 * padrão = o primeiro da organização. Escopo sempre validado pelo servidor.
 */
export function useSelectedEstablishment(orgId: string | null | undefined) {
  const query = useOrgEstablishments(orgId)
  const [sp, setSp] = useSearchParams()
  const items = useMemo(() => query.data?.items ?? [], [query.data])
  const wanted = sp.get('estabelecimento')
  const selected = items.find((e) => e.id === wanted) ?? items[0] ?? null
  const select = useCallback(
    (id: string) =>
      setSp(
        (prev) => {
          const next = new URLSearchParams(prev)
          next.set('estabelecimento', id)
          next.delete('page')
          return next
        },
        { replace: true },
      ),
    [setSp],
  )
  return { query, items, selected, select }
}

interface NamedLite {
  id: string
  name: string
  trade_name?: string | null
}

function useNameDirectory(domain: 'organizations' | 'establishments', path: string, enabled: boolean, prefix: string) {
  const q = useQuery({
    queryKey: qk.list(domain, { page_size: 100, directory: true }),
    queryFn: () => http.get<Paginated<NamedLite>>(path, { query: { page_size: 100 } }),
    enabled,
    staleTime: 10 * 60 * 1000,
  })
  const byId = useMemo(() => disambiguatedNames(q.data?.items ?? []), [q.data])
  const nameOf = useCallback(
    (id: string | null | undefined) => (!id ? '—' : (byId.get(id) ?? `${prefix} ${id.slice(0, 8)}`)),
    [byId, prefix],
  )
  return { nameOf, items: q.data?.items ?? [], query: q }
}

/**
 * Rótulo por id: nome fantasia (ou razão social). Quando dois itens ficariam com o mesmo rótulo,
 * acrescenta a razão social e, se ainda repetir, o início do id — evita escolher o cliente errado.
 */
export function disambiguatedNames(items: NamedLite[]): Map<string, string> {
  const base = (it: NamedLite) => it.trade_name || it.name
  const count = (labels: string[]) => labels.reduce((m, l) => m.set(l, (m.get(l) ?? 0) + 1), new Map<string, number>())
  const first = count(items.map(base))
  const second = items.map((it) => {
    const b = base(it)
    if ((first.get(b) ?? 0) < 2) return b
    return it.trade_name && it.name && it.name !== it.trade_name ? `${it.trade_name} (${it.name})` : b
  })
  const secondCount = count(second)
  const m = new Map<string, string>()
  items.forEach((it, i) => m.set(it.id, (secondCount.get(second[i]) ?? 0) > 1 ? `${second[i]} · ${it.id.slice(0, 6)}` : second[i]))
  return m
}

/** Nomes de organizações (equipe RD com `organizations.view`); sem permissão → id curto. */
export function useOrgNames() {
  const can = useCan()
  return useNameDirectory('organizations', '/organizations', can('organizations.view'), 'Empresa')
}

/** Nomes de estabelecimentos no escopo (`establishments.view`); sem permissão → id curto. */
export function useEstablishmentNames() {
  const can = useCan()
  return useNameDirectory('establishments', '/establishments', can('establishments.view'), 'Estabelecimento')
}

interface ScreenLite {
  id: string
  friendly_id?: string
  code?: string
  name?: string
}

/** Rótulo de tela "TEL-UF-XXXX · nome" (equipe/estabelecimento com `screens.view`). */
export function useScreenNames(query: Record<string, string | undefined> = {}) {
  const can = useCan()
  const enabled = can('screens.view')
  const q = useQuery({
    queryKey: qk.list('screens', { ...query, page_size: 100, directory: true }),
    queryFn: () => http.get<Paginated<ScreenLite>>('/screens', { query: { ...query, page_size: 100 } }),
    enabled,
    staleTime: 10 * 60 * 1000,
  })
  const byId = useMemo(() => {
    const m = new Map<string, string>()
    for (const s of q.data?.items ?? []) m.set(s.id, [s.friendly_id ?? s.code, s.name].filter(Boolean).join(' · '))
    return m
  }, [q.data])
  return useCallback((id: string | null | undefined) => (!id ? '—' : (byId.get(id) ?? `Tela ${id.slice(0, 8)}`)), [byId])
}

interface CampaignLiteName {
  id: string
  name: string
  org_id?: string
  status?: string
}

/** Nomes de campanhas (equipe RD com `campaigns.view`). Nunca usado no contexto do estabelecimento. */
export function useCampaignNames(enabled = true) {
  const can = useCan()
  const q = useQuery({
    queryKey: qk.list('campaigns', { page_size: 100, directory: true }),
    queryFn: () => http.get<Paginated<CampaignLiteName>>('/campaigns', { query: { page_size: 100 } }),
    enabled: enabled && can('campaigns.view'),
    staleTime: 5 * 60 * 1000,
  })
  const byId = useMemo(() => {
    const m = new Map<string, string>()
    for (const c of q.data?.items ?? []) m.set(c.id, c.name)
    return m
  }, [q.data])
  return useCallback((id: string | null | undefined) => (!id ? '—' : (byId.get(id) ?? `Campanha ${id.slice(0, 8)}`)), [byId])
}
