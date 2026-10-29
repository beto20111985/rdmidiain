import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router-dom'
import SupportTicketPage from '../SupportTicketPage'
import SupportNewPage from '../SupportNewPage'
import SupportListPage from '../SupportListPage'
import { normalizeCreativeCode, supportNewTicketPath, CREATIVE_CODE_RE } from '@/components/admin/support-links'
import { renderWithProviders } from '@/test/utils'
import { EST_CONTEXT, RD_CONTEXT, jsonResponse, mockApi, renderRoute, resetSession, signInAs, withPageMeta } from '@/test/adminTestUtils'

const BASE_TICKET = {
  id: 't1',
  number: 'CH-7Q3M2A',
  org_id: 'o1',
  establishment_id: 'e1',
  screen_id: 's1',
  creative_code: 'RD-ABCDEF',
  category: 'RETIRADA_ANUNCIO',
  title: 'Retirada do anúncio RD-ABCDEF',
  description: 'Conteúdo inadequado para o local.',
  status: 'EM_ATENDIMENTO',
  priority: 'MEDIA',
  created_by: 'u1',
  assigned: true,
  sla_due_at: '2026-10-08T15:00:00Z',
  sla_breached: false,
  first_response_at: null,
  resolved_at: null,
  closed_at: null,
  created_at: '2026-10-07T15:00:00Z',
  updated_at: '2026-10-07T15:10:00Z',
  history: [{ type: 'CRIADO', from: null, to: 'ABERTO', at: '2026-10-07T15:00:00Z' }],
}
// Formato atual do servidor: para o cliente, autor da equipe vem como "Equipe RD" e sem id.
const PUBLIC_MSG = { id: 'm1', author_id: null, author_name: 'Equipe RD', author_is_staff: true, body: 'Recebemos o pedido e vamos analisar.', is_internal: false, created_at: '2026-10-07T15:05:00Z' }
const INTERNAL_MSG = { id: 'm2', author_id: 'staff1', body: 'NOTA INTERNA: anunciante Loja Alfa, campanha Verão', is_internal: true, created_at: '2026-10-07T15:06:00Z' }

// Páginas completas com muitos campos: folga para execução paralela com as demais suítes.
vi.setConfig({ testTimeout: 20_000 })

beforeEach(() => resetSession())
afterEach(() => vi.unstubAllGlobals())

describe('Chamado — comentário interno × cliente', () => {
  it('estabelecimento: não vê comentário interno nem dados do anunciante e não tem a opção de comentário interno', async () => {
    signInAs({ permissions: ['support.create', 'support.view'], contexts: [EST_CONTEXT] })
    // Defesa em profundidade: mesmo que uma mensagem interna chegasse, a tela não a exibe ao cliente.
    mockApi((c) => (c.path === '/support/tickets/t1' ? jsonResponse({ ...BASE_TICKET, messages: [PUBLIC_MSG, INTERNAL_MSG] }) : undefined))
    renderRoute(<SupportTicketPage />, '/unidade/suporte/:id', '/unidade/suporte/t1')
    const msg = await screen.findByText(PUBLIC_MSG.body)
    expect(within(msg.closest('li')!).getByText('Equipe RD')).toBeInTheDocument()
    expect(screen.queryByText(/NOTA INTERNA/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Loja Alfa/)).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: /Comentário interno/ })).not.toBeInTheDocument()
    expect(screen.queryByText('Anunciante')).not.toBeInTheDocument()
    expect(screen.getByText('RD-ABCDEF')).toBeInTheDocument()
    // Visão do cliente: segue a situação; quem abriu/empresa solicitante é só da equipe RD.
    expect(screen.getByText('Em atendimento pela equipe RD')).toBeInTheDocument()
    expect(screen.queryByText('Empresa solicitante')).not.toBeInTheDocument()
  })

  it('cliente: "Aguardando sua resposta" quando a equipe devolve o chamado (não contradiz o histórico)', async () => {
    signInAs({ permissions: ['support.create', 'support.view'], contexts: [EST_CONTEXT] })
    mockApi((c) => (c.path === '/support/tickets/t1' ? jsonResponse({ ...BASE_TICKET, status: 'AGUARDANDO_CLIENTE', assigned: false, messages: [PUBLIC_MSG] }) : undefined))
    renderRoute(<SupportTicketPage />, '/unidade/suporte/:id', '/unidade/suporte/t1')
    expect(await screen.findByText('Aguardando sua resposta')).toBeInTheDocument()
    expect(screen.queryByText('Aguardando a equipe RD')).not.toBeInTheDocument()
  })

  it('cliente envia mensagem sempre pública (is_internal=false)', async () => {
    const user = userEvent.setup()
    signInAs({ permissions: ['support.create', 'support.view'], contexts: [EST_CONTEXT] })
    const { calls } = mockApi((c) => {
      if (c.method === 'GET' && c.path === '/support/tickets/t1') return jsonResponse({ ...BASE_TICKET, messages: [PUBLIC_MSG] })
      if (c.method === 'POST' && c.path === '/support/tickets/t1/messages') return jsonResponse({ id: 'm3', is_internal: false }, 201)
      return undefined
    })
    renderRoute(<SupportTicketPage />, '/unidade/suporte/:id', '/unidade/suporte/t1')
    await user.type(await screen.findByLabelText('Sua mensagem'), 'Obrigado!')
    await user.click(screen.getByRole('button', { name: 'Enviar mensagem' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true))
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ body: 'Obrigado!', is_internal: false })
  })

  it('equipe RD vê o comentário interno destacado e pode registrar outro', async () => {
    const user = userEvent.setup()
    signInAs({ permissions: ['support.view', 'support.manage'], contexts: [RD_CONTEXT] })
    const { calls } = mockApi((c) => {
      if (c.method === 'GET' && c.path === '/support/tickets/t1')
        return jsonResponse({
          ...BASE_TICKET,
          assigned_to: 'u1',
          creative_id: 'c1',
          campaign_id: 'k1',
          advertiser_org_id: 'o2',
          city_key: 'GO:goiania',
          creative_found: true,
          messages: [{ ...PUBLIC_MSG, author_id: 'staff1', author_name: 'Carlos Técnico' }, { ...INTERNAL_MSG, author_name: 'Carlos Técnico', author_is_staff: true }],
        })
      if (c.path === '/support/assignees?ticket_id=t1')
        return jsonResponse({ items: [{ user_id: 'staff1', name: 'Carlos Técnico', scopes: [], roles: ['technician'], open_tickets: 4 }] })
      if (c.method === 'POST' && c.path === '/support/tickets/t1/messages') return jsonResponse({ id: 'm4', is_internal: true }, 201)
      if (c.method === 'GET' && c.path === '/organizations/o1') return jsonResponse({ id: 'o1', name: 'Mercado Teste', trade_name: null })
      if (c.method === 'GET' && c.path === '/establishments/e1') return jsonResponse({ id: 'e1', name: 'Unidade Centro' })
      return undefined
    })
    renderRoute(<SupportTicketPage />, '/suporte/:id', '/suporte/t1')
    const internal = await screen.findByText(/NOTA INTERNA/)
    expect(within(internal.closest('li')!).getByText(/Interno — só equipe RD/)).toBeInTheDocument()
    expect(within(internal.closest('li')!).getByText('Carlos Técnico (Equipe RD)')).toBeInTheDocument()
    // Responsáveis vêm da rota do domínio, filtrada pela região do chamado
    expect(await screen.findByRole('combobox', { name: 'Atribuir a' })).toBeInTheDocument()
    expect(calls.some((c) => c.path === '/support/assignees?ticket_id=t1')).toBe(true)
    expect(screen.getByRole('link', { name: 'Ver empresa anunciante' })).toHaveAttribute('href', '/empresas/o2')
    // Quem abriu: empresa/unidade solicitante resolvidas pelo nome (a API devolve só os ids).
    expect(await screen.findByRole('link', { name: 'Mercado Teste' })).toHaveAttribute('href', '/empresas/o1')
    expect(await screen.findByText('Unidade Centro')).toBeInTheDocument()
    await user.click(screen.getByRole('checkbox', { name: /Comentário interno/ }))
    await user.type(screen.getByLabelText('Comentário interno'), 'Verificar contrato do anunciante')
    await user.click(screen.getByRole('button', { name: 'Registrar comentário interno' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true))
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ body: 'Verificar contrato do anunciante', is_internal: true })
  })
})

describe('Abrir chamado a partir de um código RD', () => {
  it('normaliza e valida o código (alfabeto seguro) e monta o link reutilizável', () => {
    expect(normalizeCreativeCode(' rd-abcdef ')).toBe('RD-ABCDEF')
    expect(normalizeCreativeCode('abcdef')).toBe('RD-ABCDEF')
    expect(CREATIVE_CODE_RE.test('RD-ABCDEF')).toBe(true)
    expect(CREATIVE_CODE_RE.test('RD-ABC10I')).toBe(false)
    expect(supportNewTicketPath('establishment', { code: 'abcdef', screenId: 's1' })).toBe('/unidade/suporte/novo?codigo=RD-ABCDEF&categoria=RETIRADA_ANUNCIO&tela=s1')
  })

  it('link do FE-OPS (/unidade/suporte?novo=1&…) abre o formulário pré-preenchido e envia código + tela', async () => {
    const user = userEvent.setup()
    signInAs({ permissions: ['support.create', 'support.view', 'screens.view', 'establishments.view'], contexts: [EST_CONTEXT] })
    const { calls } = mockApi((c) => {
      if (c.path === '/screens/s1') return jsonResponse({ id: 's1', name: 'Caixa 1', friendly_id: 'TEL-GO-7Q3M', establishment_id: 'e1' })
      if (c.method === 'POST' && c.path === '/support/tickets') return jsonResponse({ id: 't9', number: 'CH-NEW123' }, 201)
      if (c.path.startsWith('/support/tickets/t9')) return jsonResponse({ ...BASE_TICKET, id: 't9', number: 'CH-NEW123', messages: [] })
      return undefined
    })
    renderWithProviders(
      withPageMeta(
        <Routes>
          <Route path="/unidade/suporte" element={<SupportListPage />} />
          <Route path="/unidade/suporte/novo" element={<SupportNewPage />} />
          <Route path="/unidade/suporte/:id" element={<SupportTicketPage />} />
        </Routes>,
      ),
      { route: '/unidade/suporte?novo=1&categoria=RETIRADA_ANUNCIO&codigo=RD-ABC10I&tela=s1' },
    )
    expect(await screen.findByText('Pedido de retirada de anúncio')).toBeInTheDocument()
    expect(await screen.findByText('TEL-GO-7Q3M — Caixa 1')).toBeInTheDocument()
    const code = screen.getByLabelText(/Código do anúncio/)
    expect(code).toHaveValue('RD-ABC10I')
    await user.type(screen.getByLabelText(/Descrição/), 'Anúncio de bebida perto do caixa infantil.')
    await user.click(screen.getByRole('button', { name: 'Abrir chamado' }))
    expect(await screen.findByText(/Código inválido/)).toBeInTheDocument()
    expect(calls.some((c) => c.method === 'POST')).toBe(false)

    await user.clear(code)
    await user.type(code, 'rd-abcdef')
    await user.click(screen.getByRole('button', { name: 'Abrir chamado' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true))
    const post = calls.find((c) => c.method === 'POST')!
    expect(post.body).toMatchObject({
      category: 'RETIRADA_ANUNCIO',
      creative_code: 'RD-ABCDEF',
      screen_id: 's1',
      org_id: 'o1',
      title: 'Retirada do anúncio RD-ABCDEF',
    })
    expect(post.body).not.toHaveProperty('advertiser_org_id')
  })
})
