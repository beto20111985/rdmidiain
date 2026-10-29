/** Validações do auto-cadastro do anunciante (espelham o backend; o servidor sempre revalida). */
import type { PasswordPolicy } from './api'

const LETTER = /[A-Za-zÀ-ÿ]/
const DIGIT = /\d/

/** Mesmas regras de `app.core.passwords.password_policy_errors` com os limites de `GET /terms`. */
export function signupPasswordProblems(password: string, email: string | null | undefined, policy: PasswordPolicy): string[] {
  const out: string[] = []
  if (password.length < policy.min_length) out.push(`A senha deve ter no mínimo ${policy.min_length} caracteres.`)
  if (password.length > policy.max_length) out.push(`A senha deve ter no máximo ${policy.max_length} caracteres.`)
  if (policy.requires_letters_and_digits && (!LETTER.test(password) || !DIGIT.test(password)))
    out.push('A senha deve conter letras e números.')
  if (policy.must_not_contain_email && email) {
    const low = password.toLowerCase()
    const e = email.toLowerCase().trim()
    const local = e.split('@')[0] ?? ''
    if ((e && low.includes(e)) || (local.length >= 3 && low.includes(local))) out.push('A senha não pode conter o e-mail.')
  }
  return out
}

/** Etapa do formulário onde o erro do servidor deve ser corrigido (§15.4). */
export type SignupStep = 'empresa' | 'segmentos' | 'acesso' | 'termos' | 'revisao'

export function stepForError(code: string, fieldLoc?: string): SignupStep {
  if (fieldLoc?.startsWith('organization.segment_ids') || fieldLoc?.startsWith('organization.category_id')) return 'segmentos'
  if (fieldLoc?.startsWith('organization')) return 'empresa'
  if (fieldLoc?.startsWith('user')) return 'acesso'
  if (fieldLoc?.startsWith('consents')) return 'termos'
  switch (code) {
    case 'DOCUMENT_INVALID':
    case 'DOCUMENT_ALREADY_REGISTERED':
    case 'ADDRESS_INVALID':
      return 'empresa'
    case 'SEGMENT_NOT_FOUND':
    case 'CATEGORY_NOT_FOUND':
    case 'CATEGORY_TOO_BROAD':
      return 'segmentos'
    case 'PASSWORD_POLICY':
      return 'acesso'
    case 'CONSENT_REQUIRED':
    case 'CONSENT_VERSION_OUTDATED':
    case 'TERMS_NOT_PUBLISHED':
    case 'TERMS_VERSION_OUTDATED':
      return 'termos'
    default:
      return 'revisao'
  }
}

export const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/
