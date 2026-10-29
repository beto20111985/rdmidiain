/**
 * Pré-ordem (pai antes dos filhos; irmãos por nome, pt-BR) para listas planas com ``parent_id`` exibidas
 * como árvore recuada (taxonomia: segmentos/categorias). O servidor devolve por (profundidade, nome) —
 * todas as raízes antes de qualquer filho —, o que com recuo por ``depth`` fazia os filhos parecerem
 * pendurados no último pai (ex.: subsegmentos de "Bebidas" sob "Tabaco"; AÇÃO 004). Item cujo pai não
 * está na lista entra como raiz.
 */
export function treeOrder<T extends { id: string; parent_id: string | null; name: string }>(items: readonly T[]): T[] {
  const ids = new Set(items.map((x) => x.id))
  const byParent = new Map<string | null, T[]>()
  for (const it of items) {
    const k = it.parent_id && ids.has(it.parent_id) ? it.parent_id : null
    byParent.set(k, [...(byParent.get(k) ?? []), it])
  }
  const out: T[] = []
  const seen = new Set<string>()
  const walk = (pid: string | null) => {
    for (const it of [...(byParent.get(pid) ?? [])].sort((a, b) => a.name.localeCompare(b.name, 'pt-BR'))) {
      if (seen.has(it.id)) continue // defesa contra ciclo
      seen.add(it.id)
      out.push(it)
      walk(it.id)
    }
  }
  walk(null)
  return out
}
