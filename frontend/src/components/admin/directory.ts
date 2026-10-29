/**
 * Nomes de usuários e listas de responsáveis (sem dados inventados):
 * - `useUserDirectory`: id → nome a partir de GET /users (só para quem tem `users.view`).
 * - `useAssignableUsers`: quem pode receber um chamado/pedido. Ordem de tentativa:
 *     1) rota do domínio (GET /support/assignees?ticket_id=… — regional do chamado; ou
 *        GET /design/designers) — W-BIZ; resposta {items:[{user_id, name, …carga}]}
 *     2) GET /users/assignable?permission=… (W1; não exige users.view) — {items:[{id, name}]}
 *     3) GET /users + GET /roles filtrando perfis que contêm a permissão (exige users.view)
 *   Sem nenhuma delas: lista vazia — a tela oferece apenas "Assumir" (atribuir a si).
 */
import { useQuery } from '@tanstack/react-query'
import { http, type Paginated } from '@/lib/http'
import { isApiError } from '@/lib/errors'
import { useCan } from '@/hooks/usePermissions'
import type { Permission } from '@/lib/permissions'
import type { QueryDomain } from '@/lib/queryKeys'

export interface DirectoryUser {
  id: string
  name: string
  /** Carga atual (chamados abertos / pedidos ativos), quando a rota do domínio informa. */
  load?: number | null
  is_active?: boolean
  is_admin_master?: boolean
  restricted?: boolean
  email?: string
  assignments?: Array<{ id: string; role: string; scope_type: string; scope_id: string | null; scope_value: string | null }>
}

export interface RoleInfo {
  id: string
  slug: string
  name: string
  description: string | null
  is_system: boolean
  mfa_required: boolean
  permissions: string[]
  typical_scopes: string[]
}

export function useRoles(enabled = true) {
  const can = useCan()
  return useQuery({
    queryKey: ['roles', 'list'],
    queryFn: () => http.get<{ items: RoleInfo[] }>('/roles').then((r) => r.items),
    enabled: enabled && can('users.view'),
    staleTime: 5 * 60_000,
  })
}

export function useUserDirectory() {
  const can = useCan()
  const q = useQuery({
    queryKey: ['users', 'directory'],
    queryFn: () => http.get<Paginated<DirectoryUser>>('/users', { query: { page: 1, page_size: 100 } }),
    enabled: can('users.view'),
    staleTime: 5 * 60_000,
    retry: false,
  })
  const map = new Map<string, DirectoryUser>()
  for (const u of q.data?.items ?? []) map.set(u.id, u)
  return {
    nameOf: (id: string | null | undefined) => (id ? map.get(id)?.name ?? null : null),
    users: q.data?.items ?? [],
    isLoading: q.isLoading,
  }
}

type AssignableRaw = { items?: Array<Record<string, unknown>> } | Array<Record<string, unknown>>

function normalize(raw: AssignableRaw): DirectoryUser[] {
  const list = Array.isArray(raw) ? raw : raw.items ?? []
  return list
    .map((u) => {
      const load = typeof u.open_tickets === 'number' ? u.open_tickets : typeof u.active_requests === 'number' ? u.active_requests : null
      return { id: String(u.id ?? u.user_id ?? ''), name: String(u.name ?? u.full_name ?? ''), load }
    })
    .filter((u) => u.id && u.name)
}

async function tryGet(path: string, query?: Record<string, string>): Promise<DirectoryUser[] | null> {
  try {
    return normalize(await http.get<AssignableRaw>(path, { query }))
  } catch (e) {
    if (isApiError(e) && (e.status === 404 || e.status === 405 || e.status === 403)) return null
    throw e
  }
}

export function useAssignableUsers(
  domain: QueryDomain,
  permission: Permission,
  domainPath: string,
  enabled = true,
  domainQuery?: Record<string, string>,
) {
  const can = useCan()
  const canListUsers = can('users.view')
  return useQuery({
    queryKey: [domain, 'assignable', permission, domainPath, domainQuery ?? {}],
    enabled,
    staleTime: 60_000,
    retry: false,
    queryFn: async (): Promise<{ users: DirectoryUser[]; source: 'assignable' | 'domain' | 'users' | 'none' }> => {
      const d = await tryGet(domainPath, domainQuery)
      if (d) return { users: d, source: 'domain' }
      const a = await tryGet('/users/assignable', { permission })
      if (a) return { users: a, source: 'assignable' }
      if (!canListUsers) return { users: [], source: 'none' }
      const [roles, users] = await Promise.all([
        http.get<{ items: RoleInfo[] }>('/roles').then((r) => r.items),
        http.get<Paginated<DirectoryUser>>('/users', { query: { page: 1, page_size: 100, is_active: true } }),
      ])
      const slugs = new Set(roles.filter((r) => r.permissions.includes(permission)).map((r) => r.slug))
      const eligible = users.items.filter(
        (u) => !u.restricted && u.is_active !== false && (u.assignments ?? []).some((g) => slugs.has(g.role) && g.scope_type !== 'ORGANIZATION' && g.scope_type !== 'ESTABLISHMENT'),
      )
      return { users: eligible.map((u) => ({ id: u.id, name: u.name })), source: 'users' }
    },
  })
}
