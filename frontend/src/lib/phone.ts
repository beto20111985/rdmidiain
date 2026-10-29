/**
 * Telefone BR (DDD + número) — máscara e validação ÚNICAS do app ("Minha conta", cadastro de empresa e
 * auto-cadastro do anunciante). Mesmo critério do pagamento no servidor (``payments.payer_missing``:
 * DDD + número, ≥ 10 dígitos), com as regras de fixo/celular para orientar quem digita.
 */
import { EMPTY_VALUE } from '@/lib/format'

export const onlyDigits = (v: string) => v.replace(/\D/g, '')

/** Dígitos nacionais (DDD + número): o servidor pode guardar com DDI 55. */
export function nationalDigits(stored: string | null | undefined): string {
  const d = onlyDigits(stored ?? '')
  return d.startsWith('55') && d.length >= 12 ? d.slice(2) : d
}

/** Máscara BR enquanto digita: (DD) NNNN-NNNN (fixo) ou (DD) NNNNN-NNNN (celular). */
export function maskPhone(digits: string): string {
  const d = onlyDigits(digits).slice(0, 11)
  if (!d) return ''
  if (d.length <= 2) return `(${d}`
  const ddd = d.slice(0, 2)
  const rest = d.slice(2)
  if (rest.length <= 4) return `(${ddd}) ${rest}`
  const split = d.length === 11 ? 5 : 4
  return `(${ddd}) ${rest.slice(0, split)}-${rest.slice(split)}`
}

/** Problema do telefone digitado (com ou sem máscara) ou ``null`` se válido. */
export function phoneProblem(masked: string): string | null {
  const d = onlyDigits(masked)
  if (!d) return 'Informe o telefone com DDD.'
  if (d.length !== 10 && d.length !== 11) return 'Use (DD) NNNN-NNNN para fixo ou (DD) 9NNNN-NNNN para celular.'
  if (!/^[1-9]{2}$/.test(d.slice(0, 2))) return 'DDD inválido.'
  if (d.length === 11 && d[2] !== '9') return 'Celular com DDD tem 11 dígitos e começa com 9 depois do DDD.'
  if (d.length === 10 && !/[2-5]/.test(d[2])) return 'Telefone fixo começa com 2, 3, 4 ou 5 depois do DDD.'
  return null
}

export function displayPhone(stored: string | null | undefined): string {
  if (!stored) return EMPTY_VALUE
  const d = nationalDigits(stored)
  return d.length === 10 || d.length === 11 ? maskPhone(d) : stored
}

/** Valor mascarado depois de uma edição; apagar um caractere da máscara apaga o último dígito
 * (senão a máscara o recolocaria). */
export function nextMaskedPhone(raw: string, prev: string): string {
  let d = onlyDigits(raw)
  if (raw.length < prev.length && d === onlyDigits(prev)) d = d.slice(0, -1)
  return maskPhone(d)
}
