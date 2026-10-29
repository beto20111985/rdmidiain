import { useQuery } from '@tanstack/react-query'
import { http } from '@/lib/http'
import { qk } from '@/lib/queryKeys'

export interface FinanceSecurityStatus {
  totp_enabled: boolean
  financial_password_set: boolean
  financial_password_changed_at: string | null
}

/** Sob o domínio 'finance' (invalidado pelos eventos financeiros). */
export const securityStatusKey = qk.sub('finance', 'security-status')

export function useFinanceSecurityStatus(enabled = true) {
  return useQuery({
    queryKey: securityStatusKey,
    queryFn: () => http.get<FinanceSecurityStatus>('/financial/security/status'),
    enabled,
  })
}

/** Pronto para operar: 2FA ativo e senha financeira definida. */
export function useFinanceReady(enabled = true) {
  const query = useFinanceSecurityStatus(enabled)
  const ready = !!query.data?.totp_enabled && !!query.data?.financial_password_set
  return { query, ready }
}
