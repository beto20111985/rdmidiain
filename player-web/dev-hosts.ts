/**
 * Hosts permitidos do Vite dev server (`server.allowedHosts`) derivados de CONFIGURAÇÃO.
 *
 * Contrato de origens (AÇÃO 003 — ops/README.md §3.1): a lista de origens de desenvolvimento
 * (`RD_DEV_ALLOWED_ORIGINS` no painel, `RD_DEV_PLAYER_ALLOWED_ORIGINS` no player) vem de
 * `ops/dev/dev-origins.env` — nenhum domínio de túnel em código. Daqui sai SEMPRE um ARRAY
 * (nunca `true`): cada origem vira o seu hostname (sem esquema/porta).
 *
 * Regras (parse defensivo; entrada inválida → erro explícito, o dev server não sobe):
 * - origem exata `http(s)://host[:porta]` — sem curinga, caminho, query, fragmento ou credenciais;
 * - host avulso (lista legada, ex.: `VITE_ALLOWED_HOSTS`): hostname válido, sem porta/esquema;
 *   sufixo `.dominio.tld` só se explícito, com ≥ 2 rótulos e fora de domínios de túnel PÚBLICOS
 *   (ex.: `.ngrok-free.dev` liberaria o túnel de qualquer pessoa);
 * - entradas vazias são ignoradas; duplicatas removidas.
 *
 * Cópia idêntica de `frontend/dev-hosts.ts` (pacotes/contextos Docker separados) — manter em sincronia.
 */

/** Sufixos de serviços públicos de túnel: nunca aceitos como curinga `.sufixo`. */
export const PUBLIC_TUNNEL_SUFFIXES: readonly string[] = [
  'ngrok-free.dev',
  'ngrok-free.app',
  'ngrok.dev',
  'ngrok.app',
  'ngrok.io',
  'ngrok.pizza',
  'trycloudflare.com',
  'loca.lt',
  'localtunnel.me',
  'serveo.net',
  'serveousercontent.com',
  'localhost.run',
  'lhr.life',
  'devtunnels.ms',
  'pinggy.link',
  'pinggy.online',
]

const LABEL = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/
const IPV4 = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/

export class DevHostsError extends Error {
  constructor(source: string, position: number, reason: string) {
    super(`${source}: item ${position} inválido — ${reason}`)
    this.name = 'DevHostsError'
  }
}

function isHostname(host: string): boolean {
  if (IPV4.test(host)) return true
  if (!host || host.length > 253) return false
  return host.split('.').every((label) => LABEL.test(label))
}

function splitList(raw: string | undefined): string[] {
  return (raw ?? '')
    .split(',')
    .map((s) => s.trim())
    .filter((s) => s.length > 0)
}

/** Hostname (minúsculo, sem porta) de UMA origem exata; lança `Error(motivo)` se inválida. */
export function hostFromOrigin(entry: string): string {
  const v = entry.trim()
  if (v.includes('*')) throw new Error('curinga (*) não é permitido — use a origem exata')
  if (/\s/.test(v)) throw new Error('espaço dentro da origem')
  const m = /^(https?):\/\/([^/?#]*)(\/?)$/i.exec(v)
  if (!m) throw new Error('use http(s)://host[:porta], sem caminho, query ou fragmento')
  const authority = m[2] ?? ''
  if (authority.includes('@')) throw new Error('credenciais na origem não são permitidas')
  let url: URL
  try {
    url = new URL(v)
  } catch {
    throw new Error('origem malformada')
  }
  let host = url.hostname.toLowerCase()
  if (host.startsWith('[') && host.endsWith(']')) return host // IPv6 literal (o Vite já aceita IPs)
  host = host.replace(/\.$/, '')
  if (!isHostname(host)) throw new Error('host inválido')
  return host
}

/** Valida um host avulso (lista legada); devolve o host normalizado ou lança `Error(motivo)`. */
export function validateHost(entry: string): string {
  const v = entry.trim().toLowerCase()
  if (v.includes('*')) throw new Error('curinga (*) não é permitido')
  if (v.includes('://')) return hostFromOrigin(v)
  if (/[\s/:?#@]/.test(v)) throw new Error('host sem esquema, porta ou caminho')
  if (v.startsWith('.')) {
    const suffix = v.slice(1)
    if (!isHostname(suffix) || IPV4.test(suffix)) throw new Error('sufixo inválido')
    if (suffix.split('.').length < 2) throw new Error('sufixo amplo demais (domínio de topo)')
    if (PUBLIC_TUNNEL_SUFFIXES.some((s) => suffix === s || s.endsWith(`.${suffix}`))) {
      throw new Error('sufixo de túnel público liberaria hosts de terceiros — use o host exato')
    }
    return v
  }
  if (!isHostname(v)) throw new Error('host inválido')
  return v
}

export interface AllowedHostsInput {
  /** Hosts sempre permitidos (ex.: localhost, 127.0.0.1, nomes internos do Docker). */
  base: readonly string[]
  /** Lista CSV de ORIGENS (ex.: `RD_DEV_ALLOWED_ORIGINS`). */
  origins?: string | undefined
  originsSource?: string
  /** Lista CSV de HOSTS avulsos (compatibilidade, ex.: `VITE_ALLOWED_HOSTS`). */
  hosts?: string | undefined
  hostsSource?: string
}

/** Monta o ARRAY de `server.allowedHosts`. Lança `DevHostsError` na primeira entrada inválida. */
export function resolveAllowedHosts(input: AllowedHostsInput): string[] {
  const out: string[] = []
  const add = (h: string) => {
    if (!out.includes(h)) out.push(h)
  }
  input.base.forEach((h, i) => {
    try {
      add(validateHost(h))
    } catch (e) {
      throw new DevHostsError('base', i + 1, (e as Error).message)
    }
  })
  splitList(input.origins).forEach((o, i) => {
    try {
      add(hostFromOrigin(o))
    } catch (e) {
      throw new DevHostsError(input.originsSource ?? 'origins', i + 1, (e as Error).message)
    }
  })
  splitList(input.hosts).forEach((h, i) => {
    try {
      add(validateHost(h))
    } catch (e) {
      throw new DevHostsError(input.hostsSource ?? 'hosts', i + 1, (e as Error).message)
    }
  })
  return out
}
