/**
 * APOIO DE TESTE (Vitest) — estados de primeiro acesso e perfis de Master no formato do contrato
 * (SPEC AÇÃO 003 §3.1). Dados fictícios; nunca importado por código de produção.
 */
import type { ApprovalModeCode, Me, SecondMasterStatus, SetupState } from '@/features/auth/types'
import { makeMe } from './utils'

export const DONE_SETUP: SetupState = {
  required: false,
  next_step: null,
  steps: { password_change: 'DONE', totp: 'NOT_REQUIRED', financial_password: 'NOT_REQUIRED', second_master: 'NOT_REQUIRED' },
  is_admin_master: false,
  approval_mode: null,
  dual_approval_ready: null,
}

export function makeSetup(partial: Partial<Omit<SetupState, 'steps'>> & { steps?: Partial<SetupState['steps']> } = {}): SetupState {
  const { steps, ...rest } = partial
  return { ...DONE_SETUP, ...rest, steps: { ...DONE_SETUP.steps, ...steps } }
}

/** Master recém-criado pelo bootstrap: senha provisória, sem TOTP, sem senha financeira. */
export function freshMasterSetup(mode: ApprovalModeCode = 'DUAL'): SetupState {
  return makeSetup({
    required: true,
    next_step: 'PASSWORD_CHANGE',
    steps: { password_change: 'PENDING', totp: 'PENDING', financial_password: 'PENDING', second_master: mode === 'DUAL' ? 'NOT_INVITED' : 'NOT_REQUIRED' },
    is_admin_master: true,
    approval_mode: mode,
    dual_approval_ready: mode === 'SINGLE' ? null : false,
  })
}

/** Master com o primeiro acesso completo. */
export function masterSetup(mode: ApprovalModeCode, second: SecondMasterStatus): SetupState {
  const dual = mode === 'DUAL'
  return makeSetup({
    required: false,
    next_step: dual && second !== 'DONE' ? 'SECOND_MASTER' : null,
    steps: { password_change: 'DONE', totp: 'DONE', financial_password: 'DONE', second_master: dual ? second : 'NOT_REQUIRED' },
    is_admin_master: true,
    approval_mode: mode,
    dual_approval_ready: dual ? second === 'DONE' : null,
  })
}

export const MASTER_ID = 'aaaaaaaa-0000-4000-8000-000000000001'

export function makeMasterMe(mode: ApprovalModeCode, second: SecondMasterStatus = 'NOT_INVITED', extra: Partial<Me> = {}): Me {
  return makeMe({
    permissions: ['settings.view', 'settings.manage', 'finance.view', 'finance.approve', 'dashboard.view'],
    user: { id: MASTER_ID, name: 'Sócio A', email: 'a@rd.invalid', mfa_enabled: true, is_admin_master: true, theme: null },
    security_setup: masterSetup(mode, second),
    ...extra,
  })
}
