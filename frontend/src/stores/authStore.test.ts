import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useAuthStore } from './authStore'
import { useContextStore } from './contextStore'
import { tokenStore, getActiveContextHeader } from '@/lib/http'
import { headersOf, jsonResponse, makeMe, mockFetch } from '@/test/utils'

const ME = makeMe({
  permissions: ['dashboard.view', 'screens.view'],
  contexts: [
    { key: 'est:1', label: 'Minha unidade — Mercado X', kind: 'ESTABLISHMENT', org_id: '1' },
    { key: 'adv:1', label: 'Minhas campanhas — Mercado X', kind: 'ADVERTISER', org_id: '1' },
  ],
})

beforeEach(() => {
  tokenStore.clear()
  useContextStore.getState().reset()
  useAuthStore.setState({ status: 'anonymous', me: null, mfaToken: null, mfaExpiresAt: null, notice: null, demo: false })
})
afterEach(() => vi.unstubAllGlobals())

describe('authStore — fluxos do contrato v1 §2', () => {
  it('login ok: guarda o access token só em memória, carrega /me e aplica o contexto', async () => {
    mockFetch((url) => {
      if (url.endsWith('/auth/login')) return jsonResponse({ status: 'ok', access_token: 'A1', expires_in: 900 })
      if (url.endsWith('/me')) return jsonResponse(ME)
      return jsonResponse({}, 404)
    })
    const outcome = await useAuthStore.getState().login('dono@mercado.x', 'senha-segura-1')
    expect(outcome).toBe('ok')
    const s = useAuthStore.getState()
    expect(s.status).toBe('authenticated')
    expect(s.me?.permissions).toContain('screens.view')
    expect(tokenStore.get()).toBe('A1')
    expect(window.localStorage.length).toBe(0)
    expect(getActiveContextHeader()).toBe('est:1')
  })

  it('mfa_required: guarda o mfa_token e conclui com /auth/mfa/verify', async () => {
    const fetchMock = mockFetch((url, init) => {
      if (url.endsWith('/auth/login')) return jsonResponse({ status: 'mfa_required', mfa_token: 'M1', expires_in: 300 })
      if (url.endsWith('/auth/mfa/verify')) {
        const body = JSON.parse(String(init.body))
        return body.code === '123456' && body.mfa_token === 'M1'
          ? jsonResponse({ status: 'ok', access_token: 'A2', expires_in: 900 })
          : jsonResponse({ error: { code: 'MFA_INVALID', message: 'Código inválido.' } }, 401)
      }
      if (url.endsWith('/me')) return jsonResponse(ME)
      return jsonResponse({}, 404)
    })
    expect(await useAuthStore.getState().login('admin@rd', 'x')).toBe('mfa_required')
    expect(useAuthStore.getState().status).toBe('mfa_required')
    expect(useAuthStore.getState().mfaToken).toBe('M1')
    expect(tokenStore.get()).toBeNull()

    await expect(useAuthStore.getState().verifyMfa('000000')).rejects.toMatchObject({ status: 401 })
    expect(useAuthStore.getState().status).toBe('mfa_required')

    expect(await useAuthStore.getState().verifyMfa('123456')).toBe('ok')
    expect(useAuthStore.getState().status).toBe('authenticated')
    expect(useAuthStore.getState().mfaToken).toBeNull()
    expect(tokenStore.get()).toBe('A2')
    const verifyCall = fetchMock.mock.calls.filter(([u]) => String(u).endsWith('/auth/mfa/verify'))
    expect(verifyCall).toHaveLength(2)
  })

  it('mfa_setup_required: guarda o mfa_setup_token (não é access token) e o usa só nas rotas de setup; enable devolve a sessão', async () => {
    const fetchMock = mockFetch((url, init) => {
      if (url.endsWith('/auth/login')) return jsonResponse({ status: 'mfa_setup_required', mfa_setup_token: 'S1', expires_in: 600 })
      if (url.endsWith('/auth/mfa/totp/setup')) return jsonResponse({ otpauth_uri: 'otpauth://totp/RD:x?secret=ABC', secret: 'ABC' })
      if (url.endsWith('/auth/mfa/totp/enable'))
        return jsonResponse({ recovery_codes: ['AAAA-BBBB'], status: 'ok', access_token: 'FULL', expires_in: 900 })
      if (url.endsWith('/me')) return headersOf(init).Authorization === 'Bearer FULL' ? jsonResponse(ME) : jsonResponse({}, 401)
      return jsonResponse({}, 404)
    })
    expect(await useAuthStore.getState().login('fin@rd', 'x')).toBe('setup_required')
    expect(useAuthStore.getState().status).toBe('setup_required')
    expect(useAuthStore.getState().mfaSetupToken).toBe('S1')
    expect(tokenStore.get()).toBeNull()

    const { authApi } = await import('@/features/auth/api')
    await authApi.totpSetup('x', 'S1')
    const enable = await authApi.totpEnable('123456', 'S1')
    const setupCall = fetchMock.mock.calls.find(([u]) => String(u).endsWith('/auth/mfa/totp/setup'))!
    expect(headersOf(setupCall[1]!).Authorization).toBe('Bearer S1')
    expect(headersOf(setupCall[1]!)['X-RD-Context']).toBeUndefined()
    const enableCall = fetchMock.mock.calls.find(([u]) => String(u).endsWith('/auth/mfa/totp/enable'))!
    expect(enableCall[1]?.credentials).toBe('include') // aceita o Set-Cookie da sessão completa

    await useAuthStore.getState().completeMfaSetup(enable)
    expect(useAuthStore.getState().status).toBe('authenticated')
    expect(useAuthStore.getState().mfaSetupToken).toBeNull()
    expect(tokenStore.get()).toBe('FULL')
  })

  it('logout em outra aba encerra a sessão desta aba', () => {
    useAuthStore.setState({ status: 'authenticated', me: ME })
    useAuthStore.getState().remoteLogout()
    expect(useAuthStore.getState().status).toBe('anonymous')
    expect(useAuthStore.getState().notice).toBe('logged_out')
  })

  it('/me com 2FA obrigatório e não configurado resulta em setup_required (primeiro acesso)', async () => {
    mockFetch((url) => {
      if (url.endsWith('/auth/login')) return jsonResponse({ status: 'ok', access_token: 'A3', expires_in: 900 })
      if (url.endsWith('/me')) return jsonResponse({ ...ME, mfa: { required: true, configured: false } })
      return jsonResponse({}, 404)
    })
    expect(await useAuthStore.getState().login('adm@rd', 'x')).toBe('setup_required')
    expect(useAuthStore.getState().status).toBe('setup_required')
  })

  it('bootstrap: sem cookie de refresh → anônimo; com cookie → autenticado (sessão real, não flag local)', async () => {
    mockFetch(() => jsonResponse({ error: { code: 'NO_SESSION', message: 'x' } }, 401))
    await useAuthStore.getState().bootstrap()
    expect(useAuthStore.getState().status).toBe('anonymous')

    vi.unstubAllGlobals()
    mockFetch((url) => (url.endsWith('/auth/refresh') ? jsonResponse({ access_token: 'B1', expires_in: 900 }) : jsonResponse(ME)))
    await useAuthStore.getState().bootstrap()
    expect(useAuthStore.getState().status).toBe('authenticated')
  })

  it('logout revoga a sessão no servidor (cookie + CSRF) e limpa o estado local', async () => {
    tokenStore.set('A1')
    useAuthStore.setState({ status: 'authenticated', me: ME })
    const fetchMock = mockFetch(() => new Response(null, { status: 204 }))
    await useAuthStore.getState().logout()
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/auth/logout')
    expect(init?.credentials).toBe('include')
    expect(headersOf(init!)['X-RD-CSRF']).toBe('1')
    expect(useAuthStore.getState().status).toBe('anonymous')
    expect(useAuthStore.getState().notice).toBe('logged_out')
    expect(tokenStore.get()).toBeNull()
  })

  it('expireSession leva ao estado anônimo com aviso de sessão expirada', () => {
    useAuthStore.setState({ status: 'authenticated', me: ME })
    useAuthStore.getState().expireSession()
    expect(useAuthStore.getState().status).toBe('anonymous')
    expect(useAuthStore.getState().notice).toBe('expired')
  })
})
