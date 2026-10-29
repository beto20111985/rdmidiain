/**
 * Primeiro acesso no authStore (SPEC AÇÃO 003 §11.1/§11.9): estado `setup_required`, token de setup só
 * em memória (renovado a cada troca de senha), sessão nova da ativação do 2º Master e erros globais 403.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { waitFor } from '@testing-library/react'
import { useAuthStore } from './authStore'
import { useContextStore } from './contextStore'
import { tokenStore } from '@/lib/http'
import { ApiError } from '@/lib/errors'
import { queryClient } from '@/lib/queryClient'
import { useSecurityUiStore } from '@/features/security/uiStore'
import { fetchRouter } from '@/components/finance/testing'
import { freshMasterSetup, makeMasterMe, makeSetup } from '@/test/securityFixtures'
import { jsonResponse, makeMe } from '@/test/utils'

beforeEach(() => {
  tokenStore.clear()
  useContextStore.getState().reset()
  useSecurityUiStore.getState().reset()
  useAuthStore.setState({ status: 'anonymous', me: null, mfaToken: null, mfaExpiresAt: null, mfaSetupToken: null, setup: null, notice: null, demo: false })
})
afterEach(() => {
  vi.unstubAllGlobals()
  queryClient.clear()
})

describe('authStore — primeiro acesso', () => {
  it('login com mfa_setup_required guarda o estado de setup e o token SÓ em memória', async () => {
    fetchRouter([
      { method: 'POST', match: /\/auth\/login$/, reply: () => ({ status: 'mfa_setup_required', mfa_setup_token: 'SETUP-1', expires_in: 600, setup: freshMasterSetup('DUAL') }) },
    ])
    expect(await useAuthStore.getState().login('master@rd.invalid', 'x')).toBe('setup_required')
    const s = useAuthStore.getState()
    expect(s.status).toBe('setup_required')
    expect(s.setup?.next_step).toBe('PASSWORD_CHANGE')
    expect(s.mfaSetupToken).toBe('SETUP-1')
    expect(tokenStore.get()).toBeNull()
    expect(window.localStorage.length).toBe(0)
    expect(window.sessionStorage.length).toBe(0)
  })

  it('a troca de senha devolve um NOVO token de setup (o anterior deixa de valer) e o próximo passo', async () => {
    useAuthStore.setState({ status: 'setup_required', mfaSetupToken: 'SETUP-1', setup: freshMasterSetup('DUAL') })
    const next = makeSetup({ ...freshMasterSetup('DUAL'), next_step: 'TOTP', steps: { password_change: 'DONE', totp: 'PENDING', financial_password: 'PENDING', second_master: 'NOT_INVITED' } })
    await useAuthStore.getState().applySetupPassword({ status: 'mfa_setup_required', mfa_setup_token: 'SETUP-2', expires_in: 600, setup: next })
    expect(useAuthStore.getState().mfaSetupToken).toBe('SETUP-2')
    expect(useAuthStore.getState().setup?.next_step).toBe('TOTP')
    expect(useAuthStore.getState().status).toBe('setup_required')
  })

  it('/me com security_setup.required=true → setup_required; sem passo pendente → authenticated', async () => {
    const required = makeMasterMe('DUAL', 'NOT_INVITED', { security_setup: freshMasterSetup('DUAL') })
    fetchRouter([
      { method: 'POST', match: /\/auth\/login$/, reply: () => ({ status: 'ok', access_token: 'A1', expires_in: 900 }) },
      { match: /\/me$/, reply: () => required },
    ])
    expect(await useAuthStore.getState().login('master@rd.invalid', 'x')).toBe('setup_required')
    expect(useAuthStore.getState().status).toBe('setup_required')
    expect(tokenStore.get()).toBe('A1')

    vi.unstubAllGlobals()
    fetchRouter([{ match: /\/me$/, reply: () => makeMasterMe('SINGLE') }])
    await useAuthStore.getState().reloadMe()
    expect(useAuthStore.getState().status).toBe('authenticated')
    expect(useAuthStore.getState().setup?.required).toBe(false)
  })

  it('refreshSetup usa o token de setup antes da sessão (GET /auth/setup/status com Bearer do setup)', async () => {
    const r = fetchRouter([{ match: /\/auth\/setup\/status$/, reply: () => freshMasterSetup('SINGLE') }])
    useAuthStore.setState({ status: 'setup_required', mfaSetupToken: 'SETUP-1', setup: null })
    const setup = await useAuthStore.getState().refreshSetup()
    expect(setup?.approval_mode).toBe('SINGLE')
    expect(r.calls[0].headers.Authorization).toBe('Bearer SETUP-1')
    expect(r.calls[0].headers['X-RD-Context']).toBeUndefined()
  })

  it('sessão nova da ativação (adoptSession) limpa o que era da sessão anterior', async () => {
    fetchRouter([{ match: /\/me$/, reply: () => makeMasterMe('DUAL', 'DONE') }])
    tokenStore.set('OLD')
    queryClient.setQueryData(['finance', 'x'], { secreto: 'da-sessao-anterior' })
    useSecurityUiStore.getState().dismissSecondMasterModal()
    await useAuthStore.getState().adoptSession('NEW')
    expect(tokenStore.get()).toBe('NEW')
    expect(queryClient.getQueryData(['finance', 'x'])).toBeUndefined()
    expect(useSecurityUiStore.getState().secondMasterModalDismissed).toBe(false)
    expect(useAuthStore.getState().status).toBe('authenticated')
  })

  it('403 MFA_SETUP_REQUIRED de qualquer query recarrega /me e leva ao assistente (sem laço)', async () => {
    const r = fetchRouter([{ match: /\/me$/, reply: () => makeMasterMe('DUAL', 'NOT_INVITED', { security_setup: freshMasterSetup('DUAL') }) }])
    useAuthStore.setState({ status: 'authenticated', me: makeMasterMe('DUAL') })
    tokenStore.set('A1')
    const fail = () =>
      queryClient.fetchQuery({
        queryKey: ['finance', 'overview'],
        queryFn: () => Promise.reject(new ApiError({ status: 403, code: 'MFA_SETUP_REQUIRED', message: 'Ative a verificação em duas etapas.', details: { next_step: 'TOTP' } })),
        retry: false,
      })
    await expect(fail()).rejects.toBeInstanceOf(ApiError)
    await waitFor(() => expect(useAuthStore.getState().status).toBe('setup_required'))
    expect(r.find('GET', /\/me$/)).toHaveLength(1)

    // outro 403 logo em seguida não dispara nova recarga (intervalo mínimo / já em setup)
    queryClient.removeQueries({ queryKey: ['finance', 'overview'] })
    await expect(fail()).rejects.toBeInstanceOf(ApiError)
    expect(r.find('GET', /\/me$/)).toHaveLength(1)
  })

  it('403 comum (sem permissão) NÃO dispara recarga de /me', async () => {
    const r = fetchRouter([{ match: /\/me$/, reply: () => jsonResponse(makeMe()) }])
    useAuthStore.setState({ status: 'authenticated', me: makeMe() })
    await expect(
      queryClient.fetchQuery({
        queryKey: ['finance', 'outro'],
        queryFn: () => Promise.reject(new ApiError({ status: 403, code: 'FORBIDDEN', message: 'Sem permissão.' })),
        retry: false,
      }),
    ).rejects.toBeInstanceOf(ApiError)
    expect(r.find('GET', /\/me$/)).toHaveLength(0)
    expect(useAuthStore.getState().status).toBe('authenticated')
  })

  it('expireSetupToken volta ao login com aviso e apaga o token', () => {
    useAuthStore.setState({ status: 'setup_required', mfaSetupToken: 'SETUP-1', setup: freshMasterSetup('DUAL') })
    useAuthStore.getState().expireSetupToken()
    const s = useAuthStore.getState()
    expect(s.status).toBe('anonymous')
    expect(s.notice).toBe('expired')
    expect(s.mfaSetupToken).toBeNull()
    expect(s.setup).toBeNull()
  })
})
