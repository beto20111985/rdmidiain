import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { http, onSessionExpired, refreshAccessToken, setActiveContextHeader, tokenStore } from './http'
import { ApiError } from './errors'
import { headersOf, jsonResponse, mockFetch } from '@/test/utils'

beforeEach(() => {
  tokenStore.clear()
  setActiveContextHeader(null)
})
afterEach(() => {
  vi.unstubAllGlobals()
})

describe('cliente HTTP — refresh', () => {
  it('várias requisições com 401 simultâneo disparam UM ÚNICO refresh e todas são repetidas', async () => {
    tokenStore.set('velho')
    let refreshCalls = 0
    const fetchMock = mockFetch(async (url, init) => {
      if (url.endsWith('/auth/refresh')) {
        refreshCalls++
        await new Promise((r) => setTimeout(r, 20))
        return jsonResponse({ access_token: 'novo', expires_in: 900 })
      }
      const auth = headersOf(init).Authorization
      return auth === 'Bearer novo' ? jsonResponse({ ok: url }) : jsonResponse({ error: { code: 'AUTH', message: 'x' } }, 401)
    })

    const results = await Promise.all([http.get('/a'), http.get('/b'), http.get('/c')])
    expect(refreshCalls).toBe(1)
    expect(results).toEqual([{ ok: '/api/v1/a' }, { ok: '/api/v1/b' }, { ok: '/api/v1/c' }])
    expect(tokenStore.get()).toBe('novo')

    const refreshCall = fetchMock.mock.calls.find(([u]) => String(u).endsWith('/auth/refresh'))!
    expect(refreshCall[1]?.credentials).toBe('include')
    expect(headersOf(refreshCall[1]!)['X-RD-CSRF']).toBe('1')
  })

  it('refresh concorrente direto também é deduplicado', async () => {
    let calls = 0
    mockFetch(async () => {
      calls++
      await new Promise((r) => setTimeout(r, 10))
      return jsonResponse({ access_token: 't1', expires_in: 900 })
    })
    const [a, b] = await Promise.all([refreshAccessToken(), refreshAccessToken()])
    expect(calls).toBe(1)
    expect(a).toBe('t1')
    expect(b).toBe('t1')
  })

  it('serializa a renovação ENTRE ABAS com Web Locks e reaproveita o token obtido por outra aba', async () => {
    // Simula navigator.locks: a "outra aba" segura o lock e, ao liberar, já trouxe um token novo.
    let release!: () => void
    const held = new Promise<void>((r) => (release = r))
    const requested: string[] = []
    const locks = {
      request: async (name: string, cb: () => Promise<unknown>) => {
        requested.push(name)
        await held
        return cb()
      },
    }
    vi.stubGlobal('navigator', { ...window.navigator, locks })
    const fetchMock = mockFetch(() => jsonResponse({ access_token: 'nao-deveria', expires_in: 900 }))
    const p = refreshAccessToken()
    // Enquanto espera o lock, a outra aba renova e o token chega por BroadcastChannel:
    tokenStore.set('da-outra-aba')
    release()
    expect(await p).toBe('da-outra-aba')
    expect(requested).toEqual(['rd-auth-refresh'])
    expect(fetchMock).not.toHaveBeenCalled() // não girou o cookie de novo
  })

  it('refresh que falha notifica sessão expirada e propaga o 401', async () => {
    tokenStore.set('velho')
    const expired = vi.fn()
    const off = onSessionExpired(expired)
    mockFetch((url) =>
      url.endsWith('/auth/refresh') ? jsonResponse({ error: { code: 'X', message: 'x' } }, 401) : jsonResponse({}, 401),
    )
    await expect(http.get('/screens')).rejects.toMatchObject({ status: 401 })
    expect(expired).toHaveBeenCalledTimes(1)
    expect(tokenStore.get()).toBeNull()
    off()
  })
})

describe('cliente HTTP — login', () => {
  it('401 do próprio /auth/login NÃO tenta refresh e devolve a mensagem do servidor', async () => {
    const fetchMock = mockFetch(() =>
      jsonResponse({ error: { code: 'AUTH_INVALID_CREDENTIALS', message: 'E-mail, telefone ou senha incorretos.', details: {} } }, 401),
    )
    const expired = vi.fn()
    const off = onSessionExpired(expired)
    const err = (await http.post('/auth/login', { login: 'a', password: 'b' }, { auth: false }).catch((e) => e)) as ApiError
    expect(err).toBeInstanceOf(ApiError)
    expect(err.status).toBe(401)
    expect(err.code).toBe('AUTH_INVALID_CREDENTIALS')
    expect(err.message).toBe('E-mail, telefone ou senha incorretos.')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(expired).not.toHaveBeenCalled()
    off()
  })

  it('login usa credentials include (para aceitar o Set-Cookie); rotas comuns usam omit', async () => {
    tokenStore.set('t')
    const fetchMock = mockFetch(() => jsonResponse({}))
    await http.post('/auth/login', {}, { auth: false })
    await http.get('/campaigns')
    expect(fetchMock.mock.calls[0][1]?.credentials).toBe('include')
    expect(fetchMock.mock.calls[1][1]?.credentials).toBe('omit')
    expect(headersOf(fetchMock.mock.calls[1][1]!)['X-RD-CSRF']).toBeUndefined()
  })
})

describe('cliente HTTP — envelope e cabeçalhos', () => {
  it('converte o envelope de erro do contrato em ApiError (code, message, details)', async () => {
    mockFetch(() =>
      jsonResponse({ error: { code: 'CAMPAIGN_INVALID_TRANSITION', message: 'Transição inválida.', details: { from: 'DRAFT' } } }, 400),
    )
    const err = (await http.post('/campaigns/1/approve').catch((e) => e)) as ApiError
    expect(err.status).toBe(400)
    expect(err.code).toBe('CAMPAIGN_INVALID_TRANSITION')
    expect(err.message).toBe('Transição inválida.')
    expect(err.details).toEqual({ from: 'DRAFT' })
  })

  it('423 expõe retry_after_s e 429 lê Retry-After', async () => {
    mockFetch((url) =>
      url.endsWith('/x')
        ? jsonResponse({ error: { code: 'LOCKED', message: 'Bloqueado', details: { retry_after_s: 900 } } }, 423)
        : jsonResponse({ error: { code: 'RATE', message: 'Calma' } }, 429, { 'retry-after': '30' }),
    )
    const locked = (await http.get('/x').catch((e) => e)) as ApiError
    expect(locked.retryAfterSeconds).toBe(900)
    const limited = (await http.get('/y').catch((e) => e)) as ApiError
    expect(limited.retryAfterSeconds).toBe(30)
  })

  it('422 do backend v1 (details.errors) vira erros por campo', async () => {
    mockFetch(() =>
      jsonResponse(
        {
          error: {
            code: 'VALIDATION_ERROR',
            message: 'Dados inválidos.',
            details: { errors: [{ loc: ['body', 'login'], msg: 'Field required', type: 'missing' }] },
          },
        },
        422,
      ),
    )
    const err = (await http.post('/auth/login', {}, { auth: false }).catch((e) => e)) as ApiError
    expect(err.code).toBe('VALIDATION_ERROR')
    expect(err.fieldErrors).toEqual({ login: 'Field required' })
  })

  it('aceita o formato legado do FastAPI (detail) com erros por campo', async () => {
    mockFetch(() => jsonResponse({ detail: [{ loc: ['body', 'email'], msg: 'e-mail inválido' }] }, 422))
    const err = (await http.post('/users', {}).catch((e) => e)) as ApiError
    expect(err.fieldErrors).toEqual({ email: 'e-mail inválido' })
  })

  it('falha de rede vira ApiError status 0 com mensagem em pt-BR', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    const err = (await http.get('/x').catch((e) => e)) as ApiError
    expect(err.status).toBe(0)
    expect(err.message).toMatch(/Sem conexão/)
  })

  it('envia Bearer, X-Request-ID e X-RD-Context; paginação vai na query', async () => {
    tokenStore.set('abc')
    setActiveContextHeader('est:123')
    const fetchMock = mockFetch(() => jsonResponse({ items: [], total: 0, page: 2, page_size: 25 }))
    await http.get('/screens', { query: { page: 2, page_size: 25, sort: '-created_at', q: '' } })
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/screens?page=2&page_size=25&sort=-created_at')
    const h = headersOf(init!)
    expect(h.Authorization).toBe('Bearer abc')
    expect(h['X-RD-Context']).toBe('est:123')
    expect(h['X-Request-ID']).toMatch(/^[0-9a-f-]{36}$/)
  })

  it('não grava tokens no localStorage', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    mockFetch(() => jsonResponse({ access_token: 'segredo', expires_in: 900 }))
    await refreshAccessToken()
    expect(setItem).not.toHaveBeenCalled()
    expect(JSON.stringify({ ...window.localStorage })).not.toContain('segredo')
  })
})
