/**
 * Token do convite do 2º Master — vem no FRAGMENTO da URL (`/ativar-master#token=…`): o fragmento
 * não é enviado ao servidor nem aparece em logs de acesso. Aqui ele é lido UMA vez e removido da
 * barra de endereço na hora (`history.replaceState`); nunca vai para localStorage/sessionStorage,
 * query string, queryKey ou GET. Só segue no corpo (POST) das chamadas de ativação.
 */
export function takeTokenFromFragment(win: Window = window): string | null {
  const hash = win.location.hash
  if (!hash || hash.length < 2) return null
  const params = new URLSearchParams(hash.slice(1))
  if (!params.has('token')) return null
  const token = (params.get('token') ?? '').trim()
  // Remove o fragmento imediatamente (mantém caminho e query, preserva o estado do roteador).
  win.history.replaceState(win.history.state, '', `${win.location.pathname}${win.location.search}`)
  return token || null
}
