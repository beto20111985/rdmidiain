/**
 * Links públicos do suporte para outras áreas (FE-OPS: "Pedir retirada de anúncio" em
 * Minhas telas; detalhe de tela/campanha da RD). O formulário aceita:
 *   ?codigo=RD-XXXXXX  ?categoria=RETIRADA_ANUNCIO  ?tela=<screen_id>  ?estabelecimento=<id>
 * e também `/unidade/suporte?novo=1&...` (redireciona para o formulário).
 */
import type { RouteContext } from '@/stores/contextStore'

export const SUPPORT_BASE: Record<RouteContext, string> = {
  rd: '/suporte',
  establishment: '/unidade/suporte',
  advertiser: '/anunciante/suporte',
}

/** Alfabeto seguro do ID público do criativo (contrato §1 ID_PUBLICOS). */
export const CREATIVE_CODE_RE = /^RD-[ABCDEFGHJMNPQRSTUVYZ23456789]{6}$/

/** "rd-7q3m2a" / "7Q3M2A" → "RD-7Q3M2A"; null se vazio. Não valida (use CREATIVE_CODE_RE). */
export function normalizeCreativeCode(raw: string | null | undefined): string | null {
  const code = (raw ?? '').replace(/\s+/g, '').toUpperCase()
  if (!code) return null
  if (!code.startsWith('RD-') && code.length === 6) return `RD-${code}`
  return code
}

export function supportNewTicketPath(
  context: RouteContext,
  opts: { code?: string | null; category?: string; screenId?: string | null; establishmentId?: string | null } = {},
): string {
  const sp = new URLSearchParams()
  const code = normalizeCreativeCode(opts.code)
  if (code) sp.set('codigo', code)
  if (opts.category) sp.set('categoria', opts.category)
  else if (code && context === 'establishment') sp.set('categoria', 'RETIRADA_ANUNCIO')
  if (opts.screenId) sp.set('tela', opts.screenId)
  if (opts.establishmentId) sp.set('estabelecimento', opts.establishmentId)
  const qs = sp.toString()
  return `${SUPPORT_BASE[context]}/novo${qs ? `?${qs}` : ''}`
}
