/**
 * Telas que exigem Admin Master (sem conta de teste no navegador — verificação manual do
 * proprietário): white-label aplicar/restaurar, LGPD processar/recusar, exportação da
 * auditoria e gestão do control plane. Aqui: rotas chamadas e estados com/sem permissão.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import WhiteLabelPage from '../whitelabel/WhiteLabelPage'
import LgpdAdminPage from '../lgpd/LgpdAdminPage'
import AuditPage from '../audit/AuditPage'
import ControlPlanePage from '../controlplane/ControlPlanePage'
import { tokenStore } from '@/lib/http'
import { jsonResponse, mockApi, renderRoute, resetSession, signInAs, stubObjectUrls } from '@/test/adminTestUtils'

vi.setConfig({ testTimeout: 20_000 })

beforeEach(() => resetSession())
afterEach(() => {
  vi.unstubAllGlobals()
  tokenStore.clear()
})

const RD_PRIMARY = '#0056B3'
const RD_ACCENT = '#0B84FF'

function profileFixture(over: Record<string, unknown> = {}) {
  return {
    instance_slug: 'rd',
    display_name: 'RD Mídia Indoor',
    is_white_label: false,
    primary_color: RD_PRIMARY,
    accent_color: RD_ACCENT,
    has_logo: false,
    support_email: null,
    support_phone: null,
    legal_name: null,
    document: null,
    subdomain: null,
    subdomain_note: 'Subdomínio configurado na infraestrutura.',
    payment_provider: 'CLOUDPAYPAG',
    payment_credentials_ref: null,
    payment_account_label: null,
    payment_credentials: { configured: false, ref_kind: null, detail: null },
    version: 3,
    isolation: { model: 'STACK_POR_INSTANCIA', detail: 'Banco, arquivos e filas próprios.' },
    ...over,
  }
}

describe('White-label (Admin Master) — aplicar e restaurar o padrão RD', () => {
  it('PATCH /instance/profile com versão otimista; "Padrão do DS" volta às cores RD', async () => {
    const user = userEvent.setup()
    signInAs({ permissions: ['whitelabel.manage'] })
    let profile = profileFixture()
    const { calls } = mockApi((c) => {
      if (c.method === 'GET' && c.path === '/instance/profile') return jsonResponse(profile)
      if (c.method === 'GET' && c.path === '/instance/price-floors') return jsonResponse({ items: [] })
      if (c.method === 'GET' && c.path === '/instance/branding') return jsonResponse({ display_name: profile.display_name, primary_color: profile.primary_color, accent_color: profile.accent_color, has_logo: false })
      if (c.method === 'PATCH' && c.path === '/instance/profile') {
        profile = { ...profile, ...(c.body as Record<string, unknown>), version: profile.version + 1 }
        return jsonResponse(profile)
      }
      return undefined
    })
    renderRoute(<WhiteLabelPage />, '/configuracoes/white-label', '/configuracoes/white-label')

    const name = await screen.findByLabelText(/^Nome exibido/)
    await user.clear(name)
    await user.type(name, 'Rede Teste')
    const primary = screen.getByDisplayValue(RD_PRIMARY)
    await user.clear(primary)
    await user.type(primary, '#0B5A2A')
    await user.click(screen.getByRole('button', { name: 'Salvar' }))

    await waitFor(() => expect(calls.some((c) => c.method === 'PATCH')).toBe(true))
    const apply = calls.filter((c) => c.method === 'PATCH')[0]
    expect(apply.path).toBe('/instance/profile')
    expect(apply.body).toEqual({ display_name: 'Rede Teste', primary_color: '#0B5A2A', expected_version: 3 })

    // Restaurar: nome RD + "Padrão do DS" na cor principal → só os campos alterados, nova versão.
    await waitFor(() => expect(screen.getByLabelText(/^Nome exibido/)).toHaveValue('Rede Teste'))
    await waitFor(() => expect(screen.getByDisplayValue('#0B5A2A')).toBeInTheDocument())
    const name2 = screen.getByLabelText(/^Nome exibido/)
    await user.clear(name2)
    await user.type(name2, 'RD Mídia Indoor')
    await user.click(screen.getAllByRole('button', { name: 'Padrão do DS' })[0])
    await user.click(screen.getByRole('button', { name: 'Salvar' }))
    await waitFor(() => expect(calls.filter((c) => c.method === 'PATCH')).toHaveLength(2))
    expect(calls.filter((c) => c.method === 'PATCH')[1].body).toEqual({ display_name: 'RD Mídia Indoor', primary_color: RD_PRIMARY, expected_version: 4 })
  })

  it('cor sem contraste com o texto branco bloqueia o salvamento (nada é enviado)', async () => {
    const user = userEvent.setup()
    signInAs({ permissions: ['whitelabel.manage'] })
    const { calls } = mockApi((c) => {
      if (c.method === 'GET' && c.path === '/instance/profile') return jsonResponse(profileFixture())
      if (c.method === 'GET' && c.path === '/instance/price-floors') return jsonResponse({ items: [] })
      return undefined
    })
    renderRoute(<WhiteLabelPage />, '/configuracoes/white-label', '/configuracoes/white-label')
    const primary = await screen.findByDisplayValue(RD_PRIMARY)
    await user.clear(primary)
    await user.type(primary, '#FFE45C')
    expect(screen.getByRole('button', { name: 'Salvar' })).toBeDisabled()
    expect(calls.some((c) => c.method === 'PATCH')).toBe(false)
  })
})

describe('LGPD (Admin Master) — anonimizar e recusar', () => {
  const REQ = { id: 'd1', user_id: 'u9', user_name: 'Titular Teste', status: 'PENDING', reason: 'Quero sair', requested_at: '2026-10-01T12:00:00Z', processed_at: null }

  it('anonimizar exige digitar o nome do titular e chama POST …/process; recusar exige justificativa e chama POST …/reject', async () => {
    const user = userEvent.setup()
    signInAs({ permissions: ['lgpd.manage'] })
    const { calls } = mockApi((c) => {
      if (c.method === 'GET' && c.path.startsWith('/lgpd/deletion-requests')) return jsonResponse({ items: [REQ], total: 1, page: 1, page_size: 25 })
      if (c.method === 'POST' && c.path === '/lgpd/deletion-requests/d1/process') return jsonResponse({ ...REQ, status: 'COMPLETED' })
      if (c.method === 'POST' && c.path === '/lgpd/deletion-requests/d1/reject') return jsonResponse({ ...REQ, status: 'REJECTED' })
      return undefined
    })
    renderRoute(<LgpdAdminPage />, '/configuracoes/lgpd', '/configuracoes/lgpd')

    await user.click((await screen.findAllByRole('button', { name: 'Anonimizar' }))[0])
    let dialog = await screen.findByRole('dialog')
    const confirm = within(dialog).getByRole('button', { name: 'Anonimizar titular' })
    await user.click(confirm)
    expect(calls.some((c) => c.path.endsWith('/process'))).toBe(false)
    const textboxes = within(dialog).getAllByRole('textbox')
    await user.type(textboxes[textboxes.length - 1], 'Titular Teste')
    await user.click(confirm)
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/lgpd/deletion-requests/d1/process')).toBe(true))
    expect(calls.find((c) => c.path.endsWith('/process'))?.body).toEqual({ notes: null })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    await user.click(screen.getAllByRole('button', { name: 'Recusar' })[0])
    dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByRole('textbox'), 'Base legal: obrigação fiscal.')
    await user.click(within(dialog).getByRole('button', { name: 'Recusar pedido' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/lgpd/deletion-requests/d1/reject')).toBe(true))
    expect(calls.find((c) => c.path.endsWith('/reject'))?.body).toEqual({ notes: 'Base legal: obrigação fiscal.' })
  })
})

describe('Auditoria — exportação (Admin Master)', () => {
  const ROW = {
    id: 'a1', seq: 1, created_at: '2026-10-08T09:00:00Z', actor_user_id: null, real_user_id: null, actor_device_id: null, org_id: null,
    action: 'settings.update', entity_type: 'setting', entity_id: 'x', before: null, after: null, reason: null, ip: null,
    user_agent: null, origin: null, request_id: null, row_hash: 'h',
  }
  const handler = (c: { method: string; path: string }) => {
    if (c.method === 'GET' && c.path.startsWith('/audit/export'))
      return new Response('seq;acao\n1;settings.update\n', { status: 200, headers: { 'content-type': 'text/csv', 'content-disposition': 'attachment; filename="auditoria.csv"' } })
    if (c.method === 'GET' && c.path === '/audit/verify') return jsonResponse({ ok: true, rows_checked: 1, first_bad_seq: null, problem: null })
    if (c.method === 'GET' && c.path.startsWith('/audit')) return jsonResponse({ items: [ROW], total: 1, page: 1, page_size: 25 })
    if (c.method === 'GET' && c.path.startsWith('/users')) return jsonResponse({ items: [], total: 0, page: 1, page_size: 100 })
    return undefined
  }

  it('com audit.export: "Exportar CSV" chama GET /audit/export e baixa o arquivo', async () => {
    const user = userEvent.setup()
    const urls = stubObjectUrls()
    // jsdom não navega: o clique no <a download> é registrado sem tentar navegar.
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
    signInAs({ permissions: ['audit.view', 'audit.export'] })
    const { calls } = mockApi(handler)
    renderRoute(<AuditPage />, '/auditoria', '/auditoria')
    await user.click(await screen.findByRole('button', { name: 'Exportar CSV' }))
    await waitFor(() => expect(calls.some((c) => c.path.startsWith('/audit/export'))).toBe(true))
    await waitFor(() => expect(urls.create).toHaveBeenCalled())
    expect(anchorClick).toHaveBeenCalled()
    anchorClick.mockRestore()
  })

  it('sem audit.export (perfil admin): nenhum botão de exportação', async () => {
    signInAs({ permissions: ['audit.view'] })
    const { calls } = mockApi(handler)
    renderRoute(<AuditPage />, '/auditoria', '/auditoria')
    await screen.findByText('settings.update', {}, { timeout: 5000 }).catch(() => undefined)
    await waitFor(() => expect(calls.some((c) => c.path.startsWith('/audit?') || c.path === '/audit')).toBe(true))
    expect(screen.queryByRole('button', { name: 'Exportar CSV' })).not.toBeInTheDocument()
    expect(calls.some((c) => c.path.startsWith('/audit/export'))).toBe(false)
  })
})

describe('Control plane — gestão (Admin Master) × somente leitura', () => {
  const handler = (c: { method: string; path: string }) => {
    if (c.method === 'GET' && c.path === '/control-plane/overview')
      return jsonResponse({ instances: 0, by_version: {}, by_health: {}, active_rollouts: 0, latest_published_version: null, outdated: 0, execution: { real_deploy: false, detail: 'Executor externo.' } })
    if (c.method === 'GET' && c.path.startsWith('/control-plane/rollouts')) return jsonResponse({ items: [], total: 0 })
    if (c.method === 'GET' && c.path.startsWith('/control-plane/')) return jsonResponse({ items: [] })
    if (c.method === 'POST' && c.path === '/control-plane/instances')
      return jsonResponse({ id: 'i1', slug: 'rede-sul', name: 'Rede Sul', kind: 'RD_PRODUCAO', status: 'ATIVA', instance_token: 'TOKEN-UNICO-TESTE' }, 201)
    return undefined
  }

  it('com control_plane.manage: cadastra instância (POST /control-plane/instances) e mostra o token uma única vez', async () => {
    const user = userEvent.setup()
    signInAs({ permissions: ['control_plane.view', 'control_plane.manage'] })
    const { calls } = mockApi(handler)
    renderRoute(<ControlPlanePage />, '/control-plane', '/control-plane?aba=instancias')
    await user.click(await screen.findByRole('button', { name: 'Nova instância' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByLabelText(/^Identificador/), 'rede-sul')
    await user.type(within(dialog).getByLabelText(/^Nome/), 'Rede Sul')
    await user.click(within(dialog).getByRole('button', { name: 'Cadastrar instância' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/control-plane/instances')).toBe(true))
    expect(calls.find((c) => c.method === 'POST')?.body).toMatchObject({ slug: 'rede-sul', name: 'Rede Sul', kind: 'RD_PRODUCAO', group_id: null })
    expect(await within(dialog).findByText('TOKEN-UNICO-TESTE')).toBeInTheDocument()
  })

  it('só control_plane.view (perfil admin): somente leitura, sem "Nova instância"', async () => {
    signInAs({ permissions: ['control_plane.view'] })
    const { calls } = mockApi(handler)
    renderRoute(<ControlPlanePage />, '/control-plane', '/control-plane?aba=instancias')
    await waitFor(() => expect(calls.some((c) => c.path === '/control-plane/instances')).toBe(true))
    await screen.findByText('Instâncias', { selector: 'h2, h3, [class*="title"], div' }).catch(() => undefined)
    expect(screen.queryByRole('button', { name: 'Nova instância' })).not.toBeInTheDocument()
    expect(calls.some((c) => c.method !== 'GET')).toBe(false)
  })
})
