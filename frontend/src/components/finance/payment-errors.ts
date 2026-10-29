/**
 * Erros de documento/chave nos pagamentos (W-FIN — correção pós-janela do CNPJ alfanumérico).
 * - 422 PAYER_DOCUMENT_NOT_SUPPORTED_BY_GATEWAY: cadastro correto, mas a CloudPayPag ainda não aceita
 *   CNPJ alfanumérico → BLOQUEADO POR DEPENDÊNCIA EXTERNA; NÃO pedir correção, orientar o suporte.
 * - 422 PAYER_DOCUMENT_INVALID / PAYER_DATA_INCOMPLETE (details.missing): corrigir o cadastro.
 * - 422 PIX_KEY_CNPJ_ALPHANUMERIC_NOT_SUPPORTED: usar outro tipo de chave PIX.
 */
import { isApiError } from '@/lib/errors'

export interface PaymentErrorInfo {
  kind: 'external' | 'fix-registration' | 'other-pix-key'
  title: string
  message: string
}

const MISSING_LABEL: Record<string, string> = {
  document: 'CPF/CNPJ',
  name: 'nome/razão social',
  email: 'e-mail',
  phone: 'telefone',
  address: 'endereço',
}

export function paymentErrorInfo(e: unknown): PaymentErrorInfo | null {
  if (!isApiError(e)) return null
  switch (e.code) {
    case 'PAYER_DOCUMENT_NOT_SUPPORTED_BY_GATEWAY':
      return { kind: 'external', title: 'BLOQUEADO POR DEPENDÊNCIA EXTERNA', message: e.message }
    case 'PAYER_DOCUMENT_INVALID':
      return { kind: 'fix-registration', title: 'Documento do pagador inválido', message: `${e.message} Corrija o cadastro da empresa e tente de novo.` }
    case 'PAYER_DATA_INCOMPLETE': {
      const missing = Array.isArray(e.details.missing) ? (e.details.missing as unknown[]).map((m) => MISSING_LABEL[String(m)] ?? String(m)) : []
      return {
        kind: 'fix-registration',
        title: 'Cadastro do pagador incompleto',
        message: `${e.message}${missing.length ? ` Faltando: ${missing.join(', ')}.` : ''} Complete o cadastro da empresa e tente de novo.`,
      }
    }
    case 'PIX_KEY_CNPJ_ALPHANUMERIC_NOT_SUPPORTED':
      return { kind: 'other-pix-key', title: 'BLOQUEADO POR DEPENDÊNCIA EXTERNA', message: `${e.message} Use outro tipo de chave (CPF, e-mail, telefone ou aleatória).` }
    default:
      return null
  }
}

/** Mensagem para um campo de chave PIX a partir do erro do servidor (ou null se não for desse campo). */
export function pixKeyFieldError(e: unknown): string | null {
  if (!isApiError(e)) return null
  if (e.code === 'PIX_KEY_CNPJ_ALPHANUMERIC_NOT_SUPPORTED') return paymentErrorInfo(e)?.message ?? e.message
  if (e.code === 'PIX_KEY_INVALID') return e.message
  return null
}

/** CPF (11 dígitos) ou CNPJ (14 posições, alfanumérico com DV numérico) — só a forma; o servidor confere o DV. */
export function isCpfOrCnpjShape(raw: string): boolean {
  const v = stripDocumentMask(raw)
  return /^\d{11}$/.test(v) || /^[A-Z0-9]{12}\d{2}$/.test(v)
}

/** Remove apenas a máscara (". - /" e espaços) — nunca letras (CNPJ alfanumérico). */
export function stripDocumentMask(raw: string): string {
  return raw.replace(/[.\-/\s]/g, '').toUpperCase()
}
