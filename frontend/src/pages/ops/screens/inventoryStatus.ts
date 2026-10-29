/**
 * Inventário vendável da tela (AÇÃO 004, item 12). "Ativa" é o status de CADASTRO; a tela só entra na
 * disponibilidade das campanhas quando é VENDÁVEL (backend: ``screens.sellable`` — ``compute_sellable``:
 * tela Ativa/Em manutenção + estabelecimento ativo + contrato de parceria aceito). O motivo exibido é
 * derivado dos mesmos campos, na mesma precedência do diagnóstico da disponibilidade.
 */
import type { Screen } from '@/pages/ops/api/types'

export interface InventoryReason {
  /** Motivo curto (frase iniciada em maiúscula, sem ponto final). */
  reason: string
  /** Próximo passo para a equipe RD. */
  next: string
}

export function screenInventoryReason(s: Pick<Screen, 'status' | 'sellable' | 'establishment'>): InventoryReason | null {
  if (s.sellable || s.status === 'DECOMMISSIONED') return null
  if (s.establishment && !s.establishment.is_active)
    return { reason: 'Estabelecimento inativo', next: 'Reative o estabelecimento para a tela voltar ao inventário.' }
  if (s.establishment?.onboarding_status === 'AWAITING_CONTRACT')
    return {
      reason: 'Estabelecimento aguardando o aceite do contrato de parceria',
      next: 'A tela entra na disponibilidade das campanhas depois do aceite (aba “Contratos e comodato” do estabelecimento).',
    }
  if (s.status === 'INACTIVE')
    return { reason: 'Tela com status “Inativa”', next: 'Altere o status para “Ativa” (botão Status) para a tela voltar ao inventário.' }
  return null
}

/** "Estabelecimento aguardando…" → "estabelecimento aguardando…" (continua uma frase). */
export function lowerFirst(text: string): string {
  return text ? text.charAt(0).toLowerCase() + text.slice(1) : text
}
