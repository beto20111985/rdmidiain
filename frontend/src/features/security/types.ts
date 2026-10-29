/** Tipos de /security/* (SPEC AÇÃO 003 §5.2). Somente estados e nomes; nunca token, segredo ou e-mail completo de terceiros. */
import type { ApprovalModeCode } from '@/features/auth/types'

export type { ApprovalModeCode }

export interface ApprovalModeHistoryItem {
  mode_version: number
  kind: 'INITIALIZED' | 'SINGLE_TO_DUAL' | 'DUAL_TO_SINGLE' | string
  from_mode: ApprovalModeCode | null
  to_mode: ApprovalModeCode
  source: 'BOOTSTRAP_ENV' | 'LEGACY_DUAL_ADOPTED' | 'APPROVAL_REQUEST' | string
  requested_by_name: string | null
  authorized_by_name: string | null
  reason: string | null
  approval_request_id: string | null
  created_at: string
}

export interface PendingModeChange {
  id: string
  target_mode: ApprovalModeCode
  requested_by_name: string | null
  requested_at: string
  expires_at: string
}

export interface ApprovalModeInfo {
  mode: ApprovalModeCode
  mode_version: number
  initialized_at: string | null
  initialized_source: string | null
  /** boolean no modo DUAL; `null` no SINGLE (contrato §6). */
  dual_approval_ready: boolean | null
  active_masters: number
  pending_change_request: PendingModeChange | null
  /** Só titular de assento ou `audit.view` GLOBAL. */
  history: ApprovalModeHistoryItem[] | null
  integrity: { ok: boolean } | null
  env: { divergence: boolean; unsupported_var_present: boolean } | null
}

export type SeatStatus = 'ACTIVE' | 'SETUP_PENDING' | 'VACANT'

export interface MasterSeat {
  seat: number
  /** Nome apenas — e-mail/telefone do outro Master nunca são expostos. */
  name: string | null
  status: SeatStatus
  is_self: boolean
}

export interface MasterInvite {
  id: string
  seat: number
  full_name: string
  email_masked: string
  status: 'PENDING' | 'ACCEPTED' | 'REVOKED' | 'EXPIRED' | 'LOCKED' | string
  expires_at: string
  invited_by_name: string | null
  sent_count: number
}

export interface MastersInfo {
  approval_mode: ApprovalModeCode
  seats: MasterSeat[]
  invite: MasterInvite | null
  can_invite: boolean
}

export interface IntegrityReport {
  ok: boolean
  problems: string[]
}
