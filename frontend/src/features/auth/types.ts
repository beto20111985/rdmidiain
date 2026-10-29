/** Tipos do contrato v1 §2–§3 (autenticação, /me, contextos). */
import type { ThemePreference } from '@/stores/themeStore'

export type ContextKind = 'PLATFORM' | 'ESTABLISHMENT' | 'ADVERTISER'

export interface AppContextInfo {
  key: string // "rd" | "est:<org_id>" | "adv:<org_id>"
  label: string
  kind: ContextKind
  org_id?: string
}

export interface MeUser {
  id: string
  name: string
  email: string
  phone?: string | null
  mfa_enabled: boolean
  is_admin_master: boolean
  theme?: ThemePreference | null
  must_change_password?: boolean
}

export interface Assignment {
  role: string
  scope_type: 'GLOBAL' | 'ORGANIZATION' | 'ESTABLISHMENT' | 'CITY'
  scope_id?: string | null
  scope_value?: string | null
  scope_label?: string | null
}

// ---------------------------------------------------------------------------
// Primeiro acesso (AÇÃO 003 — SPEC §3.1). Somente estados e booleanos; nunca segredo.
// ---------------------------------------------------------------------------
export type ApprovalModeCode = 'SINGLE' | 'DUAL'

export type SetupStepCode = 'PASSWORD_CHANGE' | 'TOTP' | 'FINANCIAL_PASSWORD' | 'SECOND_MASTER'

/** Estado do 2º Master (só faz sentido na aprovação DUPLA, para titulares de assento). */
export type SecondMasterStatus = 'NOT_REQUIRED' | 'NOT_INVITED' | 'INVITE_PENDING' | 'OTHER_MASTER_SETUP_PENDING' | 'DONE'

export interface SetupState {
  /** Há passo BLOQUEANTE pendente (o servidor nega o resto com 403). */
  required: boolean
  next_step: SetupStepCode | null
  steps: {
    password_change: 'PENDING' | 'DONE'
    totp: 'PENDING' | 'DONE' | 'NOT_REQUIRED'
    financial_password: 'PENDING' | 'DONE' | 'NOT_REQUIRED'
    second_master: SecondMasterStatus
  }
  is_admin_master: boolean
  /** Só para titular de assento (demais: null). */
  approval_mode: ApprovalModeCode | null
  dual_approval_ready: boolean | null
}

export interface Me {
  user: MeUser
  permissions: string[]
  assignments: Assignment[]
  contexts: AppContextInfo[]
  impersonation: { active: boolean; real_user: { id: string; name: string } | null; reason?: string | null }
  mfa: { required: boolean; configured: boolean }
  /** Troca de senha obrigatória (ex.: senha provisória). */
  must_change_password?: boolean
  /**
   * Estado do primeiro acesso. O backend da AÇÃO 003 sempre envia; fica opcional no tipo só para
   * compatibilidade de fixtures e perfis de demonstração (ver `resolveSetup` em ./setup).
   */
  security_setup?: SetupState
}

export interface LoginOk {
  status: 'ok'
  access_token: string
  expires_in: number
  user?: Partial<MeUser>
}
export interface LoginMfaRequired {
  status: 'mfa_required'
  mfa_token: string
  expires_in: number
}
export interface LoginMfaSetupRequired {
  status: 'mfa_setup_required'
  /**
   * Bearer válido SOMENTE para as rotas de setup (/auth/setup/status, /auth/setup/password,
   * /auth/mfa/totp/setup e /auth/mfa/totp/enable). Só em memória.
   */
  mfa_setup_token: string
  expires_in?: number
  user?: Partial<MeUser>
  /** Estado do primeiro acesso (SPEC §3.3). */
  setup?: SetupState
}

/** POST /auth/setup/password — com token de setup devolve um NOVO token (o antigo deixa de valer). */
export type SetupPasswordResponse =
  | { status: 'mfa_setup_required'; mfa_setup_token: string; expires_in?: number; setup: SetupState }
  | { status: 'ok'; setup: SetupState }

/** POST /auth/setup/financial-password. */
export interface SetupFinancialPasswordResponse {
  status: 'ok'
  setup: SetupState
}

/** POST /auth/master-activation/inspect (pública). */
export interface MasterActivationInfo {
  full_name: string
  email_masked: string
  expires_at: string
  instance_name: string
  steps: string[]
}

/** POST /auth/master-activation/complete: sessão completa + códigos de recuperação (uma única vez). */
export interface MasterActivationResult {
  status?: 'ok'
  access_token: string
  expires_in?: number
  user?: Partial<MeUser>
  recovery_codes: string[]
}

/** Resposta de /auth/mfa/totp/enable: com mfa_setup_token traz também a sessão completa. */
export interface TotpEnableResponse {
  recovery_codes: string[]
  status?: 'ok'
  access_token?: string
  expires_in?: number
  user?: Partial<MeUser>
}
export type LoginResponse = LoginOk | LoginMfaRequired | LoginMfaSetupRequired

export interface SessionInfo {
  id: string
  created_at: string
  last_seen_at: string | null
  ip: string | null
  user_agent: string | null
  device_label: string | null
  current: boolean
}

export interface TotpSetup {
  otpauth_uri: string
  secret: string
}
