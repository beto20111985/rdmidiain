import { describe, expect, it } from 'vitest'
import { ApiError, defaultMessageFor } from '@/lib/errors'
import { freshMasterSetup, makeMasterMe, makeSetup } from '@/test/securityFixtures'
import { makeMe } from '@/test/utils'
import { isSetupRequiredError, nextStepOf, resolveSetup, secondMasterPending, wizardSteps } from './setup'
import { masterActivationErrorMessage, securityErrorMessage, setupErrorMessage, SECURITY_ERROR_MESSAGES } from './authErrors'

const err = (status: number, code: string, message = 'msg do servidor') => new ApiError({ status, code, message })

describe('primeiro acesso — regras puras', () => {
  it('os três códigos 403 do guard levam ao assistente; outros 403 não', () => {
    expect(isSetupRequiredError(err(403, 'PASSWORD_CHANGE_REQUIRED'))).toBe(true)
    expect(isSetupRequiredError(err(403, 'MFA_SETUP_REQUIRED'))).toBe(true)
    expect(isSetupRequiredError(err(403, 'FINANCIAL_PASSWORD_SETUP_REQUIRED'))).toBe(true)
    expect(isSetupRequiredError(err(403, 'FORBIDDEN'))).toBe(false)
    expect(isSetupRequiredError(err(401, 'MFA_SETUP_REQUIRED'))).toBe(false)
    expect(isSetupRequiredError(new Error('x'))).toBe(false)
  })

  it('resolveSetup usa security_setup do servidor; sem ele deriva dos campos antigos (nunca inventa 2º Master)', () => {
    const fromServer = makeSetup({ required: true, next_step: 'FINANCIAL_PASSWORD', steps: { financial_password: 'PENDING' } })
    expect(resolveSetup(makeMe({ security_setup: fromServer }))).toBe(fromServer)

    const legacyTotp = resolveSetup(makeMe({ mfa: { required: true, configured: false } }))
    expect(legacyTotp.required).toBe(true)
    expect(legacyTotp.next_step).toBe('TOTP')
    expect(legacyTotp.steps.second_master).toBe('NOT_REQUIRED')

    const legacyPassword = resolveSetup(makeMe({ must_change_password: true }))
    expect(legacyPassword.next_step).toBe('PASSWORD_CHANGE')

    const ok = resolveSetup(makeMe())
    expect(ok.required).toBe(false)
    expect(ok.next_step).toBeNull()
  })

  it('nextStepOf respeita a ordem senha → TOTP → senha financeira', () => {
    expect(nextStepOf(makeSetup({ next_step: null, steps: { password_change: 'PENDING', totp: 'PENDING' } }))).toBe('PASSWORD_CHANGE')
    expect(nextStepOf(makeSetup({ next_step: null, steps: { totp: 'PENDING', financial_password: 'PENDING' } }))).toBe('TOTP')
    expect(nextStepOf(makeSetup({ next_step: null, steps: { financial_password: 'PENDING' } }))).toBe('FINANCIAL_PASSWORD')
    expect(nextStepOf(makeSetup())).toBeNull()
  })

  it('2º Master só existe na DUPLA, para o titular de assento, e só enquanto pendente', () => {
    const dual = (s: 'NOT_INVITED' | 'INVITE_PENDING' | 'OTHER_MASTER_SETUP_PENDING' | 'DONE') => makeMasterMe('DUAL', s).security_setup
    expect(secondMasterPending(dual('NOT_INVITED'))).toBe(true)
    expect(secondMasterPending(dual('INVITE_PENDING'))).toBe(true)
    expect(secondMasterPending(dual('OTHER_MASTER_SETUP_PENDING'))).toBe(true)
    expect(secondMasterPending(dual('DONE'))).toBe(false)
    // SINGLE: nunca, mesmo que o servidor mandasse algo estranho no campo
    expect(secondMasterPending({ ...makeMasterMe('SINGLE').security_setup!, steps: { ...makeMasterMe('SINGLE').security_setup!.steps, second_master: 'NOT_INVITED' } })).toBe(false)
    // não Master
    expect(secondMasterPending(makeSetup({ approval_mode: 'DUAL', steps: { second_master: 'NOT_INVITED' } }))).toBe(false)
    expect(secondMasterPending(undefined)).toBe(false)
  })

  it('wizardSteps: só os passos pendentes (+ concluídos já vistos); passo 4 apenas na DUPLA para Master', () => {
    // Master na DUPLA com tudo pendente: 4 passos, o primeiro é o atual
    const all = wizardSteps(freshMasterSetup('DUAL'), new Set())
    expect(all.map((s) => s.code)).toEqual(['PASSWORD_CHANGE', 'TOTP', 'FINANCIAL_PASSWORD', 'SECOND_MASTER'])
    expect(all.map((s) => s.state)).toEqual(['current', 'pending', 'pending', 'pending'])

    // Master na ÚNICA: sem passo 4
    expect(wizardSteps(freshMasterSetup('SINGLE'), new Set()).map((s) => s.code)).toEqual(['PASSWORD_CHANGE', 'TOTP', 'FINANCIAL_PASSWORD'])

    // Usuário comum só com TOTP pendente: um passo
    const onlyTotp = makeSetup({ required: true, next_step: 'TOTP', steps: { totp: 'PENDING' } })
    expect(wizardSteps(onlyTotp, new Set()).map((s) => s.code)).toEqual(['TOTP'])

    // Passos concluídos nesta visita continuam no progresso como "concluídos"
    const mid = makeSetup({ required: true, next_step: 'TOTP', steps: { totp: 'PENDING', financial_password: 'PENDING' }, is_admin_master: true, approval_mode: 'SINGLE' })
    const steps = wizardSteps(mid, new Set(['PASSWORD_CHANGE']))
    expect(steps.map((s) => `${s.code}:${s.state}`)).toEqual(['PASSWORD_CHANGE:done', 'TOTP:current', 'FINANCIAL_PASSWORD:pending'])
  })
})

describe('mensagens dos códigos novos (SPEC §6)', () => {
  it('a tabela cobre os códigos novos em pt-BR (SPEC §6 + contrato B + recusas do banco do C)', () => {
    expect(Object.keys(SECURITY_ERROR_MESSAGES)).toHaveLength(24)
    for (const c of ['APPROVER_NOT_ACTIVE_MASTER', 'SELF_AUTHORIZER_INVALID', 'REQUESTER_NOT_ACTIVE_MASTER', 'MASTER_RECENTLY_RECOVERED']) {
      expect(SECURITY_ERROR_MESSAGES[c]).toBeTruthy()
    }
    for (const m of Object.values(SECURITY_ERROR_MESSAGES)) expect(m.length).toBeGreaterThan(10)
  })

  it('usa a mensagem do servidor; sem mensagem própria cai na tabela', () => {
    expect(securityErrorMessage(err(409, 'SECOND_MASTER_REQUIRED', 'Texto do servidor.'))).toBe('Texto do servidor.')
    expect(securityErrorMessage(new ApiError({ status: 409, code: 'SECOND_MASTER_REQUIRED', message: '' }))).toMatch(/segundo Administrador Master/)
    expect(securityErrorMessage(new ApiError({ status: 503, code: 'SECURITY_INTEGRITY_ALERT', message: 'Erro no servidor. Tente novamente em alguns instantes.' }))).toMatch(/integridade/)
  })

  it('423 MASTER_RECENTLY_RECOVERED mostra até quando o financeiro fica bloqueado', () => {
    const e = new ApiError({ status: 423, code: 'MASTER_RECENTLY_RECOVERED', message: 'Credenciais recuperadas recentemente: operações financeiras bloqueadas temporariamente.', details: { blocked_until: '2026-10-09T15:00:00Z' } })
    expect(securityErrorMessage(e)).toMatch(/bloqueadas temporariamente\. Liberado em 09\/10\/2026/)
  })

  it('erros do primeiro acesso e da ativação são amigáveis e genéricos', () => {
    expect(setupErrorMessage(new ApiError({ status: 400, code: 'INVALID_PASSWORD', message: defaultMessageFor(400) }))).toBe('Senha atual incorreta.')
    expect(setupErrorMessage(err(400, 'INVALID_PASSWORD', 'Senha incorreta.'))).toBe('Senha incorreta.')
    expect(setupErrorMessage(err(409, 'SETUP_STEP_OUT_OF_ORDER', ''))).toMatch(/etapa anterior/)
    expect(masterActivationErrorMessage(err(400, 'MASTER_INVITE_INVALID', 'qualquer'))).toBe('Convite inválido ou expirado.')
    expect(masterActivationErrorMessage(err(400, 'MASTER_INVITE_LOCKED', 'qualquer'))).toMatch(/bloqueado/)
    expect(masterActivationErrorMessage(err(400, 'INVALID_MFA_CODE', 'qualquer'))).toMatch(/Código do autenticador inválido/)
  })
})
