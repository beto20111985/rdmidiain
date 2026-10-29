/**
 * Primeiro acesso — regras puras (SPEC AÇÃO 003 §3). O backend é a autoridade: o estado vem de
 * `GET /me.security_setup`, `GET /auth/setup/status` e da resposta do login; aqui só interpretamos
 * esse estado (nada é calculado a partir de segredos e nada é guardado em armazenamento do navegador).
 */
import { isApiError } from '@/lib/errors'
import type { Me, SetupState, SetupStepCode } from './types'

/** Códigos 403 do guard do servidor (SPEC §3.2) que levam ao assistente de primeiro acesso. */
export const SETUP_REQUIRED_CODES: readonly string[] = ['PASSWORD_CHANGE_REQUIRED', 'MFA_SETUP_REQUIRED', 'FINANCIAL_PASSWORD_SETUP_REQUIRED']

export function isSetupRequiredError(e: unknown): boolean {
  return isApiError(e) && e.status === 403 && SETUP_REQUIRED_CODES.includes(e.code)
}

/** Ordem imposta pelo servidor: senha → TOTP → senha financeira (bloqueantes) → 2º Master. */
export const BLOCKING_STEPS: readonly SetupStepCode[] = ['PASSWORD_CHANGE', 'TOTP', 'FINANCIAL_PASSWORD']

export const SETUP_STEP_LABEL: Record<SetupStepCode, string> = {
  PASSWORD_CHANGE: 'Alterar senha',
  TOTP: 'Configurar TOTP',
  FINANCIAL_PASSWORD: 'Criar senha financeira',
  SECOND_MASTER: 'Convidar segundo Master',
}

/**
 * Estado do primeiro acesso de um `/me`. Sem `security_setup` (fixtures antigas, perfis de
 * demonstração) deriva um estado equivalente dos campos legados — nunca marca 2º Master.
 */
export function resolveSetup(me: Me): SetupState {
  if (me.security_setup) return me.security_setup
  const mustChange = Boolean(me.must_change_password ?? me.user.must_change_password)
  const totp = me.mfa?.required ? (me.mfa.configured ? 'DONE' : 'PENDING') : 'NOT_REQUIRED'
  const next: SetupStepCode | null = mustChange ? 'PASSWORD_CHANGE' : totp === 'PENDING' ? 'TOTP' : null
  return {
    required: next !== null,
    next_step: next,
    steps: {
      password_change: mustChange ? 'PENDING' : 'DONE',
      totp,
      financial_password: 'NOT_REQUIRED',
      second_master: 'NOT_REQUIRED',
    },
    is_admin_master: false,
    approval_mode: null,
    dual_approval_ready: null,
  }
}

/** Passo bloqueante ou segundo Master pendente (o `next_step` do servidor, com fallback). */
export function nextStepOf(setup: SetupState): SetupStepCode | null {
  if (setup.next_step) return setup.next_step
  if (setup.steps.password_change === 'PENDING') return 'PASSWORD_CHANGE'
  if (setup.steps.totp === 'PENDING') return 'TOTP'
  if (setup.steps.financial_password === 'PENDING') return 'FINANCIAL_PASSWORD'
  return null
}

/** 2º Master a configurar: SÓ na aprovação DUPLA, para titular de assento. SINGLE nunca (nem texto). */
export function secondMasterPending(setup: SetupState | null | undefined): boolean {
  if (!setup || !setup.is_admin_master || setup.approval_mode !== 'DUAL') return false
  const s = setup.steps.second_master
  return s === 'NOT_INVITED' || s === 'INVITE_PENDING' || s === 'OTHER_MASTER_SETUP_PENDING'
}

export type WizardStepState = 'done' | 'current' | 'pending'

const ORDER: SetupStepCode[] = ['PASSWORD_CHANGE', 'TOTP', 'FINANCIAL_PASSWORD', 'SECOND_MASTER']

/** Quais passos entram no progresso: os pendentes agora ou já vistos como pendentes nesta visita (+ 2º Master só na DUPLA). */
export function wizardSteps(setup: SetupState, seen: ReadonlySet<SetupStepCode>): Array<{ code: SetupStepCode; state: WizardStepState }> {
  const pending = new Set<SetupStepCode>()
  if (setup.steps.password_change === 'PENDING') pending.add('PASSWORD_CHANGE')
  if (setup.steps.totp === 'PENDING') pending.add('TOTP')
  if (setup.steps.financial_password === 'PENDING') pending.add('FINANCIAL_PASSWORD')
  const showSecond = setup.is_admin_master && setup.approval_mode === 'DUAL' && setup.steps.second_master !== 'NOT_REQUIRED'
  const current = nextStepOf(setup)
  return ORDER.filter((code) => (code === 'SECOND_MASTER' ? showSecond : pending.has(code) || seen.has(code))).map((code) => {
    if (code === current) return { code, state: 'current' as const }
    const done =
      code === 'PASSWORD_CHANGE'
        ? setup.steps.password_change === 'DONE'
        : code === 'TOTP'
          ? setup.steps.totp === 'DONE'
          : code === 'FINANCIAL_PASSWORD'
            ? setup.steps.financial_password === 'DONE'
            : setup.steps.second_master === 'DONE' || setup.steps.second_master === 'INVITE_PENDING'
    return { code, state: done ? ('done' as const) : ('pending' as const) }
  })
}

