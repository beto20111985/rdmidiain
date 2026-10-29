/**
 * AÇÃO 003 (agente F) — `server.allowedHosts` do Vite derivado de RD_DEV_ALLOWED_ORIGINS.
 * Domínios de túnel FICTÍCIOS (nenhum domínio real de conta em código).
 */
import { describe, expect, it } from 'vitest'
import { DevHostsError, hostFromOrigin, resolveAllowedHosts, validateHost } from '../../dev-hosts'

const TUNNEL_HOST = 'rd-teste-tunel.ngrok-free.dev'
const BASE = ['localhost', '127.0.0.1'] as const

/** Reproduz a regra do Vite 5.4 para hosts NÃO-IP (IPs/localhost/*.localhost o Vite já aceita). */
function viteAllows(allowed: readonly string[], hostHeader: string): boolean {
  const hostname = hostHeader.replace(/:\d+$/, '')
  if (hostname === 'localhost' || hostname.endsWith('.localhost')) return true
  return allowed.some((h) => h === hostname || (h.startsWith('.') && (h.slice(1) === hostname || hostname.endsWith(h))))
}

describe('resolveAllowedHosts (painel)', () => {
  it('sem configuração: somente localhost e 127.0.0.1 — sempre um array, nunca `true`', () => {
    const hosts = resolveAllowedHosts({ base: BASE, origins: undefined })
    expect(Array.isArray(hosts)).toBe(true)
    expect(hosts).toEqual(['localhost', '127.0.0.1'])
    expect(viteAllows(hosts, TUNNEL_HOST)).toBe(false)
  })

  it('origens de desenvolvimento viram hosts exatos (sem esquema/porta, sem duplicatas)', () => {
    const hosts = resolveAllowedHosts({
      base: BASE,
      origins: ` http://localhost, http://127.0.0.1:80 ,https://${TUNNEL_HOST.toUpperCase()}/,, https://${TUNNEL_HOST}:443`,
    })
    expect(hosts).toEqual(['localhost', '127.0.0.1', TUNNEL_HOST])
    expect(viteAllows(hosts, 'localhost')).toBe(true)
    expect(viteAllows(hosts, TUNNEL_HOST)).toBe(true)
    expect(viteAllows(hosts, `${TUNNEL_HOST}:443`)).toBe(true)
    // host não autorizado continua bloqueado — inclusive outro túnel do mesmo provedor
    for (const bad of ['malicioso.example.com', 'outro.ngrok-free.dev', `x.${TUNNEL_HOST}`, `${TUNNEL_HOST}.evil.com`]) {
      expect(viteAllows(hosts, bad)).toBe(false)
    }
  })

  it.each([
    ['*'],
    ['https://*.ngrok-free.dev'],
    [TUNNEL_HOST], // sem esquema: a variável é de ORIGENS
    ['ftp://exemplo.com'],
    ['https://exemplo.com/painel'],
    ['https://exemplo.com?x=1'],
    ['https://exemplo.com#x'],
    ['https://user:pw@exemplo.com'],
    ['https://exa mple.com'],
    ['https://-exemplo.com'],
    ['https://exemplo.com:99999'],
  ])('rejeita origem inválida %s com erro explícito', (bad) => {
    expect(() => resolveAllowedHosts({ base: BASE, origins: `http://localhost,${bad}`, originsSource: 'RD_DEV_ALLOWED_ORIGINS' }))
      .toThrowError(DevHostsError)
    expect(() => resolveAllowedHosts({ base: BASE, origins: `http://localhost,${bad}`, originsSource: 'RD_DEV_ALLOWED_ORIGINS' }))
      .toThrowError(/RD_DEV_ALLOWED_ORIGINS: item 2/)
  })
})

describe('validateHost / hostFromOrigin', () => {
  it('aceita sufixo explícito de domínio próprio, recusa curingas amplos e de túnel público', () => {
    expect(validateHost('.dev.cliente-exemplo.com.br')).toBe('.dev.cliente-exemplo.com.br')
    for (const bad of ['.ngrok-free.dev', '.ngrok-free.app', '.ngrok.io', '.trycloudflare.com', '.dev', '.com', '.', '*', '*.x.com', 'host:8088', 'a/b']) {
      expect(() => validateHost(bad)).toThrow()
    }
  })

  it('hostFromOrigin normaliza', () => {
    expect(hostFromOrigin('HTTPS://Rd-Teste-Tunel.Ngrok-Free.Dev:8443')).toBe(TUNNEL_HOST)
    expect(hostFromOrigin('http://[::1]:5173')).toBe('[::1]')
  })
})
