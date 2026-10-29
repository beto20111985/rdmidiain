/**
 * Mensagens pt-BR para códigos de erro do domínio que pedem contexto além do envelope:
 * - Piso de preço RD (white-label): `PRICE_BELOW_RD_MINIMUM` (400) — contrato ÚNICO do W-CAMP/W-BIZ,
 *   details `{floor_key, min_price_cents, price_cents}`; o mínimo é exibido em BRL.
 * - Documento do pagador (W-FIN): `PAYER_DATA_INCOMPLETE`, `PAYER_DOCUMENT_INVALID`,
 *   `PAYER_DOCUMENT_NOT_SUPPORTED_BY_GATEWAY` (CNPJ alfanumérico).
 * - Alteração aprovada sem capacidade: `CHANGE_NO_CAPACITY` (409; reserva anterior intacta).
 * Demais erros: a mensagem do envelope do servidor.
 */
import { errorMessage, isApiError } from '@/lib/errors'
import { formatCents } from '@/lib/format'

export const PRICE_FLOOR_CODE = 'PRICE_BELOW_RD_MINIMUM'
export const PAYER_CODES = ['PAYER_DATA_INCOMPLETE', 'PAYER_DOCUMENT_INVALID', 'PAYER_DOCUMENT_NOT_SUPPORTED_BY_GATEWAY'] as const

export const CHANGE_NO_CAPACITY_TEXT =
  'Não há capacidade nas telas para aplicar esta alteração agora. A versão atual continua no ar, a reserva anterior foi mantida e a alteração segue em análise — ajuste a alteração ou tente aprovar mais tarde.'

export function isPriceFloorError(err: unknown): boolean {
  return isApiError(err) && err.code === PRICE_FLOOR_CODE
}

export function isPayerError(err: unknown): boolean {
  return isApiError(err) && err.code.startsWith('PAYER_')
}

/** Erros que pertencem ao fluxo de cobrança (exibidos com `CheckoutError`). */
export function isPaymentFlowError(err: unknown): boolean {
  return (
    isApiError(err) &&
    (err.code === 'INVENTORY_UNAVAILABLE' ||
      err.code === 'PAYMENT_CREATION_FAILED' ||
      err.status === 503 ||
      isPayerError(err) ||
      isPriceFloorError(err))
  )
}

/** Texto do piso de preço com o mínimo em BRL (a partir de `min_price_cents`). */
export function priceFloorText(err: unknown): string | null {
  if (!isApiError(err) || !isPriceFloorError(err)) return null
  const d = err.details as { min_price_cents?: number; price_cents?: number; floor_key?: string }
  if (typeof d.min_price_cents !== 'number') return err.message
  const price = typeof d.price_cents === 'number' ? ` e o valor calculado foi ${formatCents(d.price_cents)}` : ''
  return `Preço abaixo do mínimo da RD: o mínimo é ${formatCents(d.min_price_cents)}${price}. Ajuste preços, descontos ou a configuração.`
}

/** Mensagem por código (para itens de lote que trazem só `code`/`message`). */
export function codeText(code: string | null | undefined, fallback: string): string {
  if (code === 'CHANGE_NO_CAPACITY') return CHANGE_NO_CAPACITY_TEXT
  return fallback
}

/**
 * Executa a ação e, em erro com texto de domínio (ex.: CHANGE_NO_CAPACITY, piso de preço), relança
 * com a mensagem traduzida — para diálogos da fundação que exibem `errorMessage(erro)`.
 */
export async function withDescribedError<T>(fn: () => Promise<T>): Promise<T> {
  try {
    return await fn()
  } catch (e) {
    const text = describeError(e)
    if (isApiError(e) && text !== e.message) throw new Error(text)
    throw e
  }
}

/** Mensagem a exibir para qualquer erro do domínio (envelope do servidor, com contexto quando há). */
export function describeError(err: unknown): string {
  if (isApiError(err) && err.code === 'CHANGE_NO_CAPACITY') return CHANGE_NO_CAPACITY_TEXT
  return priceFloorText(err) ?? errorMessage(err)
}
