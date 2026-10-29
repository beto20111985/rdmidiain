import { useQuery } from '@tanstack/react-query'
import { qk } from '@/lib/queryKeys'
import { securityApi } from './api'

export const approvalModeKey = qk.sub('security', 'approval-mode')
export const mastersKey = qk.sub('security', 'masters')

/** Modo de aprovação financeira da instância (settings.view ou finance.view). Invalidado por `security.status_changed`. */
export function useApprovalModeInfo(enabled = true) {
  return useQuery({ queryKey: approvalModeKey, queryFn: securityApi.approvalMode, enabled })
}

/** Assentos e convite do 2º Master (somente Master com o primeiro acesso completo). */
export function useMastersInfo(enabled = true) {
  return useQuery({ queryKey: mastersKey, queryFn: securityApi.masters, enabled })
}
