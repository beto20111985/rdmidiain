import { describe, expect, it } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'
import { APP_ROUTES, CONTEXT_HOME, NAV_GROUPS, NAV_SECTIONS, ROUTES, firstAccessiblePath, visibleNavRoutes } from './routes'
import { buildNav, activeNavRouteId } from './navigation'
import { PERMISSIONS, isKnownPermission } from '@/lib/permissions'

describe('routes.ts — fonte única de rotas/menus', () => {
  it('toda rota exige uma permissão VÁLIDA do catálogo do contrato (ou só autenticação)', () => {
    for (const r of ROUTES) {
      if (r.permission !== null) expect(isKnownPermission(r.permission), `${r.id} → ${r.permission}`).toBe(true)
    }
  })

  // O contrato fica fora de frontend/ (no container só frontend/ é montado em /app): o teste
  // roda quando o arquivo está acessível e é pulado caso contrário.
  const CONTRACT = path.resolve(__dirname, '../../../docs/contracts/API_CONVENCOES_AUTH_RBAC_REALTIME_v1.md')
  it.skipIf(!fs.existsSync(CONTRACT))('o catálogo do frontend é idêntico ao do contrato v1 §3.1', () => {
    const contract = fs.readFileSync(CONTRACT, 'utf8')
    const line = contract.split('\n').find((l) => l.includes('`dashboard.view`'))!
    const codes = new Set<string>()
    for (const m of line.matchAll(/`([a-z_]+)\.([a-z_|]+)`/g)) {
      for (const action of m[2].split('|')) codes.add(`${m[1]}.${action}`)
    }
    expect([...codes].sort()).toEqual([...PERMISSIONS].sort())
  })

  it('ids e caminhos são únicos; contextos, seções e grupos são válidos', () => {
    expect(new Set(ROUTES.map((r) => r.id)).size).toBe(ROUTES.length)
    expect(new Set(ROUTES.map((r) => r.path)).size).toBe(ROUTES.length)
    const sections = new Set(NAV_SECTIONS.map((s) => s.id))
    const groups = new Set(NAV_GROUPS.map((g) => g.id))
    for (const r of ROUTES) {
      expect(r.contexts.length, r.id).toBeGreaterThan(0)
      r.contexts.forEach((c) => expect(['rd', 'establishment', 'advertiser']).toContain(c))
      if (r.nav) {
        expect(sections.has(r.nav.section), r.id).toBe(true)
        if (r.nav.group) expect(groups.has(r.nav.group), r.id).toBe(true)
      }
      expect(r.summary.length, r.id).toBeGreaterThan(10)
    }
  })

  it('declara todos os menus previstos (A3 §13 + pedido W-FE)', () => {
    const paths = new Set(APP_ROUTES.map((r) => r.path))
    const required = [
      '/visao-geral', '/estabelecimentos', '/telas', '/telas/:id', '/telas/parear', '/biblioteca', '/conteudo-interno',
      '/programacao', '/inventario', '/campanhas', '/campanhas/nova', '/campanhas/:id', '/campanhas/aprovacoes',
      '/taxonomia', '/precos', '/precos/cupons', '/precos/margem', '/financeiro', '/financeiro/recebiveis',
      '/financeiro/pagaveis', '/financeiro/despesas', '/financeiro/fluxo-de-caixa', '/financeiro/dre',
      '/financeiro/fechamento', '/financeiro/extratos', '/financeiro/conciliacao', '/financeiro/comissoes',
      '/financeiro/saques', '/financeiro/chaves-pix', '/financeiro/aprovacoes', '/crm/leads', '/crm/funil',
      '/crm/propostas', '/design/briefings', '/design/fila', '/design/versoes', '/design/templates', '/suporte',
      '/estoque', '/estoque/manutencao', '/relatorios', '/notificacoes', '/usuarios', '/usuarios/perfis', '/auditoria',
      '/configuracoes', '/configuracoes/seguranca', '/configuracoes/white-label', '/configuracoes/lgpd', '/control-plane',
      '/unidade', '/unidade/telas', '/unidade/programacao', '/unidade/comissoes', '/unidade/financeiro/saques',
      '/unidade/relatorios', '/unidade/suporte',
      '/anunciante', '/anunciante/campanhas', '/anunciante/campanhas/nova', '/anunciante/checkout/:id',
      '/anunciante/faturas', '/anunciante/comprovantes', '/anunciante/design', '/anunciante/suporte',
      '/minha-conta', '/minha-conta/seguranca', '/minha-conta/sessoes',
    ]
    for (const p of required) expect(paths.has(p), p).toBe(true)
  })

  it('o web player não faz parte do painel', () => {
    expect(ROUTES.some((r) => r.path.startsWith('/player'))).toBe(false)
  })

  it('a visão geral do estabelecimento não oferece saque (Financeiro → Saques) — P001 §86', () => {
    const est = APP_ROUTES.find((r) => r.id === 'est.dashboard')!
    expect(est.permission).not.toBe('withdrawals.request')
    expect(APP_ROUTES.find((r) => r.id === 'est.withdrawals')!.path).toBe('/unidade/financeiro/saques')
  })

  it('o menu é filtrado por permissão e contexto', () => {
    const owner = ['dashboard.view', 'screens.view', 'withdrawals.view']
    const nav = visibleNavRoutes('establishment', owner).map((r) => r.id)
    expect(nav).toContain('est.dashboard')
    expect(nav).toContain('est.screens')
    expect(nav).toContain('est.withdrawals')
    expect(nav).not.toContain('rd.overview')
    expect(nav).not.toContain('est.commissions')
    expect(visibleNavRoutes('rd', owner).map((r) => r.id)).not.toContain('rd.finance')
    expect(firstAccessiblePath('establishment', owner)).toBe(CONTEXT_HOME.establishment)
  })

  it('grupos com um único item visível viram item simples; item ativo pelo prefixo mais longo', () => {
    const sections = buildNav('rd', ['finance.view', 'withdrawals.view', 'screens.view'])
    const fin = sections.find((s) => s.section.id === 'financeiro')!
    expect(fin.nodes[0].kind).toBe('group')
    const ops = sections.find((s) => s.section.id === 'operacao')!
    expect(ops.nodes.every((n) => n.kind === 'item')).toBe(true) // só "Todas as telas"
    const all = visibleNavRoutes('rd', [...PERMISSIONS])
    expect(activeNavRouteId('/telas/abc', all)).toBe('rd.screens')
    expect(activeNavRouteId('/telas/parear', all)).toBe('rd.screens.pairing')
    expect(activeNavRouteId('/financeiro/saques', all)).toBe('rd.finance.withdrawals')
  })
})
