/** Mensagens e atalhos dos erros de criação/decisão de pedidos de aprovação financeira (sem JSX). */
import { errorMessage, isApiError } from '@/lib/errors'
import { securityErrorMessage } from '@/features/auth/authErrors'

export function approvalErrorMessage(error: unknown): string {
  if (typeof error === 'string') return error
  return isApiError(error) ? securityErrorMessage(error) : errorMessage(error)
}

export function isSecondMasterRequired(error: unknown): boolean {
  return isApiError(error) && error.code === 'SECOND_MASTER_REQUIRED'
}

/** Caminho da tela onde o 2º Master é convidado/gerenciado. */
export const SECURITY_SETTINGS_PATH = '/configuracoes/seguranca'
