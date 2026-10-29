/**
 * Rótulo de pessoa a partir do NOME resolvido pelo servidor (`*_name` — CRM e aprovações) e do id.
 * Sem nome (ex.: usuário removido sem rótulo) → identificador curto, nunca um nome inventado.
 */
export function personLabel(name: string | null | undefined, id: string | null | undefined, meId?: string | null): string {
  if (!id) return '—'
  const base = name || `Usuário ${id.slice(0, 8)}`
  return meId && id === meId ? `${base} (você)` : base
}
