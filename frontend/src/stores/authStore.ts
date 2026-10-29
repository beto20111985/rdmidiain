/**
 * Sessão do usuário — contrato v1 §2–§3 e SPEC AÇÃO 003 §3/§11.
 * Estado derivado da sessão REAL (refresh no boot + GET /me), nunca de flag persistida.
 * Nada aqui é salvo em armazenamento do navegador (mfa_token e mfa_setup_token só em memória).
 *
 * `setup_required`: há passo BLOQUEANTE do primeiro acesso pendente (troca de senha, TOTP ou senha
 * financeira). Vem da resposta `mfa_setup_required` do login (ainda sem sessão) ou de
 * `/me.security_setup.required` (com sessão). O assistente `/primeiro-acesso` conduz os passos.
 */
import { create } from 'zustand'
import { broadcastLogout, refreshAccessToken, tokenStore } from '@/lib/http'
import { isApiError } from '@/lib/errors'
import { queryClient, setSetupRequiredHandler } from '@/lib/queryClient'
import { qk } from '@/lib/queryKeys'
import { authApi } from '@/features/auth/api'
import { resolveSetup } from '@/features/auth/setup'
import type { LoginResponse, Me, SetupPasswordResponse, SetupState, TotpEnableResponse } from '@/features/auth/types'
import { useSecurityUiStore } from '@/features/security/uiStore'
import { useContextStore } from './contextStore'
import { isThemePreference, useThemeStore } from './themeStore'

export type AuthStatus =
  | 'booting'
  | 'anonymous'
  | 'mfa_required'
  | 'setup_required'
  | 'authenticated'
  | 'unavailable'

export type AuthNotice = 'expired' | 'logged_out' | 'mfa_enabled' | 'password_reset' | null

export type LoginOutcome = 'ok' | 'mfa_required' | 'setup_required'

interface AuthState {
  status: AuthStatus
  me: Me | null
  mfaToken: string | null
  mfaExpiresAt: number | null
  /** Bearer restrito às rotas de setup do primeiro acesso (antes de existir sessão). Só em memória. */
  mfaSetupToken: string | null
  /** Último estado do primeiro acesso conhecido (login, /auth/setup/*, /me). */
  setup: SetupState | null
  notice: AuthNotice
  /** Modo DEMONSTRAÇÃO (somente dev): sessão fictícia para revisão visual, sem backend. */
  demo: boolean
  bootstrap: () => Promise<void>
  login: (login: string, password: string) => Promise<LoginOutcome>
  verifyMfa: (code: string) => Promise<LoginOutcome>
  acceptInvite: (token: string, name: string, password: string) => Promise<LoginOutcome>
  /** Após /auth/mfa/totp/enable: adota a sessão devolvida (fluxo obrigatório) e carrega /me. */
  completeMfaSetup: (enableResponse?: TotpEnableResponse) => Promise<void>
  /** Resultado de POST /auth/setup/password: novo token de setup (sem sessão) ou sessão atualizada. */
  applySetupPassword: (res: SetupPasswordResponse) => Promise<void>
  /** Consulta GET /auth/setup/status (token de setup) ou /me (sessão) e atualiza o estado. */
  refreshSetup: () => Promise<SetupState | null>
  /** O token de setup expirou (10 min): volta ao login com aviso. */
  expireSetupToken: () => void
  /** Sessão nova vinda da ativação do 2º Master (substitui qualquer sessão anterior desta aba). */
  adoptSession: (accessToken: string) => Promise<void>
  /** 403 do guard do servidor (senha/TOTP/senha financeira pendentes): recarrega /me → assistente. */
  handleSetupRequired: () => Promise<void>
  reloadMe: () => Promise<void>
  logout: () => Promise<void>
  logoutAll: () => Promise<void>
  /** Sessão expirou (refresh falhou após 401): limpa tudo e avisa na tela de login. */
  expireSession: () => void
  /** Outra aba fez logout. */
  remoteLogout: () => void
  /** Token renovado: se havia impersonation, recarrega /me (o token de impersonation pode ter expirado). */
  onTokenRefreshed: () => void
  endImpersonation: () => Promise<void>
  setNotice: (n: AuthNotice) => void
  /** Somente em desenvolvimento: entra com um /me fictício rotulado como DEMONSTRAÇÃO. */
  enterDemo: (me: Me) => void
}

function applyMe(me: Me) {
  queryClient.setQueryData(qk.me, me)
  useContextStore.getState().syncFromMe(me.contexts ?? [])
  const theme = me.user?.theme
  if (isThemePreference(theme)) useThemeStore.getState().applyServerPreference(theme)
}

function statusFromMe(me: Me): AuthStatus {
  return resolveSetup(me).required ? 'setup_required' : 'authenticated'
}

/** Login sem `setup` na resposta (servidor antigo): o token de setup só serve ao TOTP. */
const TOTP_ONLY_SETUP: SetupState = {
  required: true,
  next_step: 'TOTP',
  steps: { password_change: 'DONE', totp: 'PENDING', financial_password: 'NOT_REQUIRED', second_master: 'NOT_REQUIRED' },
  is_admin_master: false,
  approval_mode: null,
  dual_approval_ready: null,
}

/** Boot em andamento (StrictMode/efeitos duplicados compartilham a mesma verificação). */
let bootInFlight: Promise<void> | null = null
/** Recarga de /me disparada por 403 de setup (uma por vez, com intervalo mínimo contra laços). */
let setupReloadInFlight: Promise<void> | null = null
let lastSetupReloadAt = 0
const SETUP_RELOAD_MIN_INTERVAL_MS = 3000

export const useAuthStore = create<AuthState>((set, get) => {
  const clearLocal = (notice: AuthNotice) => {
    tokenStore.clear()
    queryClient.clear()
    useContextStore.getState().reset()
    useSecurityUiStore.getState().reset()
    set({
      status: 'anonymous',
      me: null,
      mfaToken: null,
      mfaExpiresAt: null,
      mfaSetupToken: null,
      setup: null,
      notice,
      demo: false,
    })
  }

  const loadMe = async (): Promise<AuthStatus> => {
    try {
      const me = await authApi.me()
      applyMe(me)
      const status = statusFromMe(me)
      set({ me, status, setup: resolveSetup(me) })
      return status
    } catch (e) {
      if (isApiError(e) && (e.status === 401 || e.status === 403)) {
        clearLocal(null)
        return 'anonymous'
      }
      set({ status: 'unavailable' })
      throw e
    }
  }

  const handleLoginResponse = async (res: LoginResponse): Promise<LoginOutcome> => {
    if (res.status === 'mfa_required') {
      set({
        status: 'mfa_required',
        mfaToken: res.mfa_token,
        mfaExpiresAt: Date.now() + (res.expires_in ?? 300) * 1000,
        mfaSetupToken: null,
        notice: null,
      })
      return 'mfa_required'
    }
    if (res.status === 'mfa_setup_required') {
      tokenStore.clear()
      set({
        status: 'setup_required',
        mfaToken: null,
        mfaSetupToken: res.mfa_setup_token,
        setup: res.setup ?? TOTP_ONLY_SETUP,
        notice: null,
      })
      return 'setup_required'
    }
    tokenStore.set(res.access_token)
    set({ mfaToken: null, mfaExpiresAt: null, mfaSetupToken: null, notice: null })
    const status = await loadMe()
    return status === 'setup_required' ? 'setup_required' : 'ok'
  }

  return {
    status: 'booting',
    me: null,
    mfaToken: null,
    mfaExpiresAt: null,
    mfaSetupToken: null,
    setup: null,
    notice: null,
    demo: false,

    bootstrap: () => {
      if (bootInFlight) return bootInFlight
      bootInFlight = (async () => {
        set({ status: 'booting' })
        const token = await refreshAccessToken()
        if (get().demo) return // sessão de demonstração (dev) iniciada durante o boot
        if (!token) {
          set({ status: 'anonymous', me: null })
          return
        }
        try {
          await loadMe()
        } catch {
          /* status 'unavailable' já aplicado */
        }
      })().finally(() => {
        bootInFlight = null
      })
      return bootInFlight
    },

    login: async (login, password) => {
      const res = await authApi.login(login, password)
      return handleLoginResponse(res)
    },

    verifyMfa: async (code) => {
      const token = get().mfaToken
      if (!token) throw new Error('Sessão de verificação expirada. Entre novamente.')
      const res = await authApi.verifyMfa(token, code)
      return handleLoginResponse(res)
    },

    acceptInvite: async (token, name, password) => {
      const res = await authApi.acceptInvite(token, name, password)
      return handleLoginResponse(res)
    },

    completeMfaSetup: async (enableResponse) => {
      if (enableResponse?.access_token) {
        tokenStore.set(enableResponse.access_token)
        set({ mfaSetupToken: null })
      } else {
        set({ mfaSetupToken: null })
        // Sessão já existente (ativação voluntária) ou servidor sem sessão na resposta.
        if (!tokenStore.get()) await refreshAccessToken()
      }
      if (tokenStore.get()) {
        try {
          // Com sessão, o passo seguinte (senha financeira) vem de /me.security_setup.
          const status = await loadMe()
          if (status === 'authenticated' || status === 'setup_required') return
        } catch {
          /* cai no fluxo de novo login */
        }
      }
      clearLocal('mfa_enabled')
    },

    applySetupPassword: async (res) => {
      if (res.status === 'mfa_setup_required') {
        // O token anterior deixou de valer (pwd_ts): guarda o novo, só em memória.
        set({ status: 'setup_required', mfaSetupToken: res.mfa_setup_token, setup: res.setup })
        return
      }
      set({ setup: res.setup })
      await loadMe()
    },

    refreshSetup: async () => {
      const setupToken = get().mfaSetupToken
      if (setupToken && !tokenStore.get()) {
        const setup = await authApi.setupStatus(setupToken)
        set({ setup })
        return setup
      }
      if (!tokenStore.get()) return null
      await loadMe()
      return get().setup
    },

    expireSetupToken: () => clearLocal('expired'),

    adoptSession: async (accessToken) => {
      // Sessão nova (outra pessoa): nada da sessão anterior desta aba pode ficar em memória.
      queryClient.clear()
      useContextStore.getState().reset()
      useSecurityUiStore.getState().reset()
      tokenStore.set(accessToken)
      set({ mfaToken: null, mfaExpiresAt: null, mfaSetupToken: null, notice: null, demo: false })
      await loadMe()
    },

    handleSetupRequired: () => {
      if (get().status !== 'authenticated' || get().demo) return Promise.resolve()
      if (setupReloadInFlight) return setupReloadInFlight
      if (Date.now() - lastSetupReloadAt < SETUP_RELOAD_MIN_INTERVAL_MS) return Promise.resolve()
      lastSetupReloadAt = Date.now()
      setupReloadInFlight = loadMe()
        .then(() => undefined)
        .catch(() => undefined)
        .finally(() => {
          setupReloadInFlight = null
        })
      return setupReloadInFlight
    },

    reloadMe: async () => {
      await loadMe()
    },

    logout: async () => {
      if (get().demo) {
        try {
          window.sessionStorage.removeItem('rd-dev-demo-profile')
        } catch {
          /* ignora */
        }
        clearLocal('logged_out')
        return
      }
      try {
        await authApi.logout()
      } catch {
        /* mesmo com falha de rede, a sessão local é encerrada */
      }
      broadcastLogout()
      clearLocal('logged_out')
    },

    logoutAll: async () => {
      await authApi.logoutAll()
      broadcastLogout()
      clearLocal('logged_out')
    },

    expireSession: () => {
      if (get().status === 'anonymous' || get().demo) return
      clearLocal('expired')
    },

    remoteLogout: () => {
      if (get().status === 'anonymous' || get().demo) return
      clearLocal('logged_out')
    },

    onTokenRefreshed: () => {
      if (get().me?.impersonation?.active) void loadMe().catch(() => undefined)
    },

    endImpersonation: async () => {
      await authApi.endImpersonation()
      // O token de impersonation deixa de valer: renova e recarrega a identidade real.
      tokenStore.clear()
      const token = await refreshAccessToken()
      if (!token) {
        clearLocal('logged_out')
        return
      }
      queryClient.removeQueries({ predicate: (q) => q.queryKey[0] !== 'me' })
      await loadMe()
    },

    setNotice: (notice) => set({ notice }),

    enterDemo: (me) => {
      if (!import.meta.env.DEV) return
      applyMe(me)
      set({ status: statusFromMe(me), me, setup: resolveSetup(me), demo: true, notice: null })
    },
  }
})

// Erros globais de setup (403 PASSWORD_CHANGE_REQUIRED | MFA_SETUP_REQUIRED | FINANCIAL_PASSWORD_SETUP_REQUIRED)
// vindos de qualquer query/mutation do TanStack: recarrega /me e o RequireAuth leva ao assistente.
setSetupRequiredHandler(() => void useAuthStore.getState().handleSetupRequired())
