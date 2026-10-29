/** Segurança da instância — SPEC AÇÃO 003 §5.2. O token do convite NUNCA passa por aqui (vai só ao e-mail do convidado). */
import { http, request } from '@/lib/http'
import type { ApprovalModeInfo, IntegrityReport, MasterInvite, MastersInfo } from './types'

export const securityApi = {
  approvalMode: () => http.get<ApprovalModeInfo>('/security/approval-mode'),
  masters: () => http.get<MastersInfo>('/security/masters'),
  createInvite: (body: { full_name: string; email: string; totp_code: string }) =>
    http.post<MasterInvite>('/security/masters/invites', body),
  resendInvite: (id: string) =>
    http.post<{ sent: boolean; expires_at: string }>(`/security/masters/invites/${encodeURIComponent(id)}/resend`),
  revokeInvite: (id: string, reason?: string) =>
    request<void>(`/security/masters/invites/${encodeURIComponent(id)}`, {
      method: 'DELETE',
      body: reason ? { reason } : undefined,
    }),
  integrity: () => http.get<IntegrityReport>('/security/integrity'),
}
