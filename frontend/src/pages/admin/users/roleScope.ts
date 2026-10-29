/** Perfil + escopo: valor do formulário e corpo da API (convites e atribuições). */
import { SCOPE_LABELS, type ScopeType } from '@/components/admin/labels'
import type { PickedEntity } from '@/components/admin/pickers'

export interface RoleScopeValue {
  role: string
  scope_type: ScopeType
  scope: PickedEntity | null
  uf: string
  city: string
}

export const EMPTY_ROLE_SCOPE: RoleScopeValue = { role: '', scope_type: 'GLOBAL', scope: null, uf: '', city: '' }

/** Corpo da API para o escopo escolhido; `null` se incompleto (mensagem em `problem`). */
export function roleScopePayload(v: RoleScopeValue): { body: Record<string, unknown> | null; problem: string | null } {
  if (!v.role) return { body: null, problem: 'Escolha o perfil.' }
  if (v.scope_type === 'GLOBAL') return { body: { role: v.role, scope_type: 'GLOBAL' }, problem: null }
  if (v.scope_type === 'CITY') {
    if (!v.uf || v.city.trim().length < 2) return { body: null, problem: 'Informe a UF e a cidade do escopo.' }
    return { body: { role: v.role, scope_type: 'CITY', scope_value: `${v.uf}:${v.city.trim()}` }, problem: null }
  }
  if (!v.scope) return { body: null, problem: v.scope_type === 'ORGANIZATION' ? 'Escolha a empresa do escopo.' : 'Escolha o estabelecimento do escopo.' }
  return { body: { role: v.role, scope_type: v.scope_type, scope_id: v.scope.id }, problem: null }
}

export function scopeText(a: { scope_type: string; scope_value?: string | null; scope_label?: string | null; scope_id?: string | null }): string {
  const kind = SCOPE_LABELS[a.scope_type as ScopeType] ?? a.scope_type
  if (a.scope_type === 'GLOBAL') return kind
  const fallback = a.scope_id ? `${a.scope_id.slice(0, 8)}…` : '—'
  return `${kind}: ${a.scope_label ?? a.scope_value ?? fallback}`
}
