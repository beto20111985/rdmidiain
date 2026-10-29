/**
 * Confirmação reforçada (step-up) das operações financeiras sensíveis: código TOTP do
 * aplicativo autenticador + senha financeira própria (ADR_009; P001 §83–§88).
 * O servidor valida (anti-replay do TOTP, senha financeira) — aqui só o formato.
 */
import { z } from 'zod'

export const totpSchema = z
  .string()
  .trim()
  .regex(/^\d{6}$/, 'Informe o código de 6 dígitos do aplicativo autenticador.')

export const financialPasswordSchema = z.string().min(1, 'Informe sua senha financeira.').max(128, 'Senha longa demais.')

export const emailCodeSchema = z
  .string()
  .trim()
  .regex(/^\d{6}$/, 'Informe o código de 6 dígitos enviado por e-mail.')

export const stepUpShape = {
  totp_code: totpSchema,
  financial_password: financialPasswordSchema,
}

/** Mensagens claras para os erros de step-up do backend (códigos estáveis). */
export function stepUpFieldFromError(code: string): 'totp_code' | 'financial_password' | 'email_code' | null {
  const c = code.toUpperCase()
  if (c.includes('TOTP') || c.includes('MFA')) return 'totp_code'
  if (c.includes('FINANCIAL_PASSWORD')) return 'financial_password'
  if (c.includes('EMAIL_CODE')) return 'email_code'
  return null
}
