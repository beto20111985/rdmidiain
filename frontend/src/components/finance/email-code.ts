/**
 * Código por e-mail OBRIGATÓRIO em toda aprovação financeira e em todo saque (W-FIN pós-janela d):
 * 6 dígitos, uso único, 10 min, vinculado a usuário + ação + recurso (no saque: estabelecimento e
 * VALOR). Aqui ficam o mapeamento dos erros do contrato e o contador LOCAL de reenvio (timer de UI
 * com setTimeout encadeado — não consulta o servidor; polling é proibido).
 */
import { useCallback, useEffect, useState } from 'react'
import { isApiError } from '@/lib/errors'

/** Espera mínima entre envios (UI); o servidor limita a 5 envios / 15 min (429). */
export const EMAIL_CODE_RESEND_SECONDS = 60

export type EmailCodeErrorTarget = 'email_code' | 'send' | 'form'

export interface EmailCodeErrorInfo {
  target: EmailCodeErrorTarget
  message: string
  /** Invalida o código atual (pedir outro). */
  resetCode?: boolean
  /** Segundos de espera antes de reenviar (429). */
  cooldownSeconds?: number
}

export function emailCodeErrorInfo(e: unknown): EmailCodeErrorInfo | null {
  if (!isApiError(e)) return null
  switch (e.code) {
    case 'FINANCIAL_EMAIL_CODE_REQUIRED':
      return { target: 'email_code', message: 'Envie o código por e-mail e informe os 6 dígitos recebidos.' }
    case 'FINANCIAL_EMAIL_CODE_INVALID':
      return {
        target: 'email_code',
        message: 'Código inválido, expirado, já usado ou de outra operação. Peça um novo código.',
        resetCode: true,
      }
    case 'FINANCIAL_EMAIL_CODE_RATE_LIMITED':
      return {
        target: 'send',
        message: 'Muitos envios de código em pouco tempo. Aguarde para pedir outro.',
        cooldownSeconds: e.retryAfterSeconds ?? EMAIL_CODE_RESEND_SECONDS,
      }
    case 'FINANCIAL_EMAIL_CODE_UNAVAILABLE':
      return { target: 'send', message: 'O envio/validação de códigos por e-mail está indisponível agora. Tente novamente em alguns minutos.' }
    case 'EMAIL_REQUIRED':
      return { target: 'send', message: 'Sua conta não tem e-mail cadastrado para receber o código. Atualize em Minha conta.' }
    case 'FINANCIAL_PASSWORD_RECENTLY_RESET':
      return { target: 'form', message: e.message || 'Sua senha financeira foi redefinida há menos de 24 h; operações financeiras estão bloqueadas até o fim do período.' }
    default:
      return null
  }
}

/** Contador regressivo local (1 s) para o botão "Reenviar código". */
export function useCountdown() {
  const [remaining, setRemaining] = useState(0)
  useEffect(() => {
    if (remaining <= 0) return
    const t = window.setTimeout(() => setRemaining((r) => Math.max(0, r - 1)), 1000)
    return () => window.clearTimeout(t)
  }, [remaining])
  const start = useCallback((seconds: number) => setRemaining(Math.max(0, Math.ceil(seconds))), [])
  const reset = useCallback(() => setRemaining(0), [])
  return { remaining, start, reset }
}

export function formatCountdown(seconds: number): string {
  const s = Math.max(0, Math.ceil(seconds))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}
