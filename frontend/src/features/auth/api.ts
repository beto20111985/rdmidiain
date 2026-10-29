/** Endpoints de autenticação — contrato v1 §2 e SPEC AÇÃO 003 §5. Base: /api/v1. */
import { http } from '@/lib/http'
import type {
  LoginResponse,
  MasterActivationInfo,
  MasterActivationResult,
  Me,
  SessionInfo,
  SetupFinancialPasswordResponse,
  SetupPasswordResponse,
  SetupState,
  TotpEnableResponse,
  TotpSetup,
} from './types'
import type { ThemePreference } from '@/stores/themeStore'

/** O mfa_setup_token vale só para as rotas de setup: vai como Bearer explícito, sem refresh em 401. */
function setupTokenOptions(setupToken?: string | null) {
  return setupToken
    ? { auth: false, retryOn401: false, sendContext: false, headers: { Authorization: `Bearer ${setupToken}` } }
    : undefined
}

export const authApi = {
  login: (login: string, password: string) =>
    http.post<LoginResponse>('/auth/login', { login, password }, { auth: false }),
  verifyMfa: (mfa_token: string, code: string) =>
    http.post<LoginResponse>('/auth/mfa/verify', { mfa_token, code }, { auth: false }),
  logout: () => http.post<void>('/auth/logout'),
  logoutAll: () => http.post<void>('/auth/logout-all'),
  me: () => http.get<Me>('/me'),
  updateTheme: (theme: ThemePreference) => http.patch<unknown>('/me', { theme }),
  sessions: () => http.get<SessionInfo[] | { items: SessionInfo[] }>('/auth/sessions'),
  revokeSession: (id: string) => http.delete<void>(`/auth/sessions/${encodeURIComponent(id)}`),
  forgotPassword: (email: string) => http.post<void>('/auth/password/forgot', { email }, { auth: false }),
  resetPassword: (email: string, code: string, new_password: string) =>
    http.post<void>('/auth/password/reset', { email, code, new_password }, { auth: false }),
  changePassword: (current_password: string, new_password: string) =>
    http.post<void>('/auth/password/change', { current_password, new_password }),
  acceptInvite: (token: string, name: string, password: string) =>
    http.post<LoginResponse>('/auth/invites/accept', { token, name, password }, { auth: false }),
  /** `setupToken` = mfa_setup_token do login (2FA obrigatório); sem ele usa a sessão atual. */
  totpSetup: (password: string, setupToken?: string | null) =>
    http.post<TotpSetup>('/auth/mfa/totp/setup', { password }, setupTokenOptions(setupToken)),
  totpEnable: (code: string, setupToken?: string | null) =>
    http.post<TotpEnableResponse>('/auth/mfa/totp/enable', { code }, setupTokenOptions(setupToken)),
  endImpersonation: () => http.delete<void>('/auth/impersonation'),

  // --- Primeiro acesso (SPEC §5.1) ---------------------------------------------------------
  /** Estado do primeiro acesso: token de setup (antes do TOTP) ou sessão. */
  setupStatus: (setupToken?: string | null) => http.get<SetupState>('/auth/setup/status', setupTokenOptions(setupToken)),
  /** Troca de senha do primeiro acesso. Com token de setup a resposta traz um NOVO token. */
  setupPassword: (current_password: string, new_password: string, setupToken?: string | null) =>
    http.post<SetupPasswordResponse>('/auth/setup/password', { current_password, new_password }, setupTokenOptions(setupToken)),
  /** Senha financeira do titular de assento (exige sessão). */
  setupFinancialPassword: (body: { login_password: string; totp_code: string; financial_password: string }) =>
    http.post<SetupFinancialPasswordResponse>('/auth/setup/financial-password', body),

  // --- Ativação do 2º Master (pública; token do e-mail do convite, SOMENTE no corpo) ------------
  masterActivation: {
    inspect: (token: string) => http.post<MasterActivationInfo>('/auth/master-activation/inspect', { token }, { auth: false, sendContext: false }),
    totp: (token: string) => http.post<TotpSetup>('/auth/master-activation/totp', { token }, { auth: false, sendContext: false }),
    complete: (body: { token: string; password: string; totp_code: string; financial_password: string }) =>
      // `/auth/master-activation/complete` está em COOKIE_ROUTES de lib/http.ts (credentials: 'include' p/ o cookie rd_refresh)
      http.post<MasterActivationResult>('/auth/master-activation/complete', body, { auth: false, sendContext: false }),
  },
}

/** Normaliza a lista de sessões (array puro ou envelope paginado). */
export function sessionsList(data: SessionInfo[] | { items: SessionInfo[] } | undefined): SessionInfo[] {
  if (!data) return []
  return Array.isArray(data) ? data : data.items ?? []
}

/** Política de senha do contrato §2: ≥10 caracteres, letras e números, sem conter o e-mail. */
export function passwordProblems(password: string, email?: string | null): string[] {
  const problems: string[] = []
  if (password.length < 10) problems.push('Use pelo menos 10 caracteres.')
  if (!/[A-Za-zÀ-ÿ]/.test(password) || !/\d/.test(password)) problems.push('Combine letras e números.')
  if (email) {
    const local = email.split('@')[0]?.toLowerCase()
    if (local && local.length >= 3 && password.toLowerCase().includes(local))
      problems.push('A senha não pode conter seu e-mail.')
  }
  return problems
}
