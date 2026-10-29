import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import DesignRequestDetailPage from '../DesignRequestDetailPage'
import TemplateEditorPage from '../templates/TemplateEditorPage'
import { emptyEditor, newLayer, toSpec, validateSpec, type EditorState, type TemplateSpec } from '../templates/templateSpec'
import { ADV_CONTEXT, RD_CONTEXT, jsonResponse, mockApi, renderRoute, resetSession, signInAs, stubObjectUrls } from '@/test/adminTestUtils'

const VERSION = {
  id: 'v1',
  number: 1,
  source: 'UPLOAD',
  template_id: null,
  filename: 'arte-v1.png',
  content_type: 'image/png',
  size_bytes: 204800,
  sha256: 'a'.repeat(64),
  width: 1920,
  height: 1080,
  notes: 'Primeira proposta',
  status: 'EM_REVISAO',
  locked: false,
  created_by: 'designer1',
  created_at: '2026-10-07T13:00:00Z',
}
const REQUEST = {
  id: 'r1',
  code: 'ART-7Q3M2A',
  org_id: 'o2',
  establishment_id: null,
  kind: 'ANUNCIO_EXTERNO',
  title: 'Promoção de inverno',
  briefing: 'Destacar café e pão de queijo.',
  texts: ['Café + pão de queijo R$ 9,90'],
  references: [],
  orientation: 'HORIZONTAL',
  media_type: 'IMAGEM',
  duration_s: 30,
  status: 'EM_REVISAO',
  requested_by: 'u1',
  assigned_to: 'designer1',
  billing: 'COBRAR',
  price_cents: 25000,
  payment_status: 'PAGO',
  payment_order_id: 'p1',
  revisions_included: 3,
  extra_revisions_count: 0,
  extra_revisions: 0,
  revisions_used: 0,
  revisions_remaining: 3,
  approved_version_id: null,
  approved_at: null,
  published_media_id: null,
  published_at: null,
  due_at: null,
  cancel_reason: null,
  version: 4,
  created_at: '2026-10-06T13:00:00Z',
  updated_at: '2026-10-07T13:00:00Z',
  attachments: [],
  approvals: [],
  history: [],
  extra_revision_purchases: [],
  versions: [VERSION],
}

// Páginas completas com muitos campos: folga para execução paralela com as demais suítes.
vi.setConfig({ testTimeout: 20_000 })

beforeEach(() => {
  resetSession()
  stubObjectUrls()
})
afterEach(() => vi.unstubAllGlobals())

function pngResponse() {
  return new Response(new Uint8Array([0x89, 0x50, 0x4e, 0x47]), { status: 200, headers: { 'content-type': 'image/png' } })
}

describe('Arte — aprovação formal do cliente', () => {
  it('aprovar registra a decisão e depois exibe data/hora, IP, usuário e a trava da versão', async () => {
    const user = userEvent.setup()
    const me = signInAs({ permissions: ['design.request', 'design.approve'], contexts: [ADV_CONTEXT] })
    let approved = false
    const { calls } = mockApi((c) => {
      if (c.method === 'GET' && c.path === '/design/requests/r1')
        return jsonResponse(
          approved
            ? {
                ...REQUEST,
                status: 'APROVADA',
                approved_version_id: 'v1',
                approved_at: '2026-10-07T14:30:00Z',
                versions: [{ ...VERSION, status: 'APROVADA', locked: true }],
                approvals: [
                  {
                    id: 'ap1',
                    version_id: 'v1',
                    decision: 'APROVADA',
                    comment: null,
                    user_id: me.user.id,
                    ip_address: '203.0.113.7',
                    user_agent: 'Mozilla/5.0 (teste)',
                    version_sha256: 'a'.repeat(64),
                    revision_number: null,
                    decided_at: '2026-10-07T14:30:00Z',
                  },
                ],
              }
            : REQUEST,
        )
      if (c.path === '/design/requests/r1/comments') return jsonResponse({ items: [] })
      if (c.path === '/design/requests/r1/versions/v1/file') return pngResponse()
      if (c.method === 'POST' && c.path === '/design/requests/r1/versions/v1/decision') {
        approved = true
        return jsonResponse({ ...REQUEST, status: 'APROVADA' })
      }
      return undefined
    })
    renderRoute(<DesignRequestDetailPage />, '/anunciante/design/:id', '/anunciante/design/r1')
    expect(await screen.findByText(/Revisões usadas:/)).toBeInTheDocument()
    expect(screen.getByText('0 de 3')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Aprovar formalmente' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/data e a hora, o seu usuário, o IP e o navegador/)).toBeInTheDocument()
    expect(within(dialog).getByText(/fica travada/)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Aprovar arte' }))

    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true))
    expect(calls.find((c) => c.method === 'POST')!.body).toEqual({ decision: 'APROVAR', comment: null })
    expect(await screen.findByText('Aprovação formal registrada')).toBeInTheDocument()
    const card = screen.getByText('Aprovação formal registrada').closest('[data-slot="card"]') as HTMLElement
    expect(within(card).getByText('203.0.113.7')).toBeInTheDocument()
    expect(within(card).getByText('Você')).toBeInTheDocument()
    expect(within(card).getByText('07/10/2026, 11:30')).toBeInTheDocument() // America/Sao_Paulo
    expect(screen.getByText('Travada')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Aprovar formalmente' })).not.toBeInTheDocument()
  })

  it('4ª alteração: com as 3 revisões usadas mostra o aviso e oferece revisão extra (paga)', async () => {
    signInAs({ permissions: ['design.request', 'design.approve'], contexts: [ADV_CONTEXT] })
    mockApi((c) => {
      if (c.path === '/design/requests/r1') return jsonResponse({ ...REQUEST, revisions_used: 3, revisions_remaining: 0 })
      if (c.path === '/design/requests/r1/comments') return jsonResponse({ items: [] })
      if (c.path.endsWith('/file')) return pngResponse()
      return undefined
    })
    renderRoute(<DesignRequestDetailPage />, '/anunciante/design/:id', '/anunciante/design/r1')
    expect(await screen.findByText('As 3 revisões incluídas foram usadas')).toBeInTheDocument()
    expect(screen.getByText(/A próxima alteração \(4ª\) exige uma revisão extra/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Solicitar revisão extra' })).toBeInTheDocument()
  })

  it('revisão extra liberada amplia o limite (extra_revisions_count) e lista as compras (extra_revision_purchases)', async () => {
    signInAs({ permissions: ['design.request', 'design.approve'], contexts: [ADV_CONTEXT] })
    mockApi((c) => {
      if (c.path === '/design/requests/r1')
        return jsonResponse({
          ...REQUEST,
          revisions_used: 3,
          extra_revisions_count: 1,
          extra_revisions: 1,
          revisions_remaining: 1,
          extra_revision_purchases: [
            { id: 'x1', status: 'LIBERADA', price_cents: 5000, reason: 'Trocar foto', payment_order_id: 'p2', created_at: '2026-10-07T13:30:00Z', decided_at: '2026-10-07T13:40:00Z', released_at: '2026-10-07T13:50:00Z' },
          ],
        })
      if (c.path === '/design/requests/r1/comments') return jsonResponse({ items: [] })
      if (c.path.endsWith('/file')) return pngResponse()
      return undefined
    })
    renderRoute(<DesignRequestDetailPage />, '/anunciante/design/:id', '/anunciante/design/r1')
    expect(await screen.findByText('3 de 4')).toBeInTheDocument()
    expect(screen.getByText(/3 incluídas \+ 1 extra/)).toBeInTheDocument()
    expect(screen.getByText('Revisões extras')).toBeInTheDocument()
    expect(screen.getByText('Liberada')).toBeInTheDocument()
    expect(screen.queryByText('As 3 revisões incluídas foram usadas')).not.toBeInTheDocument()
  })

  it('comentário interno do designer não aparece para o cliente', async () => {
    signInAs({ permissions: ['design.request', 'design.approve'], contexts: [ADV_CONTEXT] })
    mockApi((c) => {
      if (c.path === '/design/requests/r1') return jsonResponse(REQUEST)
      if (c.path === '/design/requests/r1/comments')
        return jsonResponse({
          items: [
            { id: 'c1', version_id: null, author_id: null, author_name: 'Equipe RD', author_is_staff: true, body: 'Versão enviada, aguardamos seu retorno.', is_internal: false, created_at: '2026-10-07T13:01:00Z' },
            { id: 'c2', version_id: null, author_id: 'designer1', body: 'INTERNO: cliente difícil', is_internal: true, created_at: '2026-10-07T13:02:00Z' },
          ],
        })
      if (c.path.endsWith('/file')) return pngResponse()
      return undefined
    })
    renderRoute(<DesignRequestDetailPage />, '/anunciante/design/:id', '/anunciante/design/r1')
    const pub = await screen.findByText('Versão enviada, aguardamos seu retorno.')
    expect(within(pub.closest('li')!).getByText('Equipe RD')).toBeInTheDocument()
    expect(screen.queryByText(/INTERNO: cliente difícil/)).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: /Comentário interno/ })).not.toBeInTheDocument()
  })
})

describe('Editor de templates — spec válida', () => {
  it('o estado inicial e camadas novas geram spec aceita pelas regras do servidor', () => {
    const state: EditorState = emptyEditor()
    state.layers.push(newLayer('rect'), newLayer('text'))
    const spec = toSpec(state)
    expect(validateSpec(spec)).toEqual([])
    expect(spec.layers.every((l) => !('uid' in l))).toBe(true)
    expect(spec.background.color).toMatch(/^#[0-9A-F]{6}$/)
  })

  it('detecta caixa fora da grade 24×24, variável não declarada, fonte não permitida e imagem sem arquivo', () => {
    const spec: TemplateSpec = {
      background: { color: '#05070A' },
      variables: [{ name: 'preco', label: 'Preço', max_len: 20 }],
      layers: [
        { type: 'rect', box: { col: 20, row: 0, col_span: 10, row_span: 2 }, color: '#0B84FF', opacity: 100, radius_px: 0 },
        { type: 'text', box: { col: 0, row: 0, col_span: 24, row_span: 4 }, text: 'Só hoje {{preco}} {{validade}}', font: 'comic-sans', size_px: 64, color: '#FFFFFF', align: 'left', valign: 'top', max_lines: 2, uppercase: false },
        { type: 'image', box: { col: 0, row: 0, col_span: 4, row_span: 4 }, asset_id: '', fit: 'contain' },
      ],
    }
    const problems = validateSpec(spec)
    expect(problems).toContain('Camada 1: a caixa ultrapassa a grade 24×24.')
    expect(problems).toContain('Camada 2: variáveis não declaradas: validade.')
    expect(problems).toContain('Camada 2: fonte não permitida.')
    expect(problems).toContain('Camada 3: envie a imagem da camada.')
  })

  it('o editor por formulário envia POST /templates com spec válida (grade, tokens e orientação)', async () => {
    const user = userEvent.setup()
    signInAs({ permissions: ['templates.manage', 'design.view', 'design.work'], contexts: [RD_CONTEXT] })
    const { calls } = mockApi((c) => {
      if (c.path === '/templates/meta')
        return jsonResponse({
          grid: 24,
          canvases: { HORIZONTAL: { width: 1920, height: 1080 }, VERTICAL: { width: 1080, height: 1920 } },
          fonts: ['dejavu-mono', 'dejavu-sans', 'dejavu-sans-bold', 'dejavu-serif', 'dejavu-serif-bold'],
          layer_types: ['text', 'image', 'rect'],
          limits: { layers: 30, variables: 20, font_px: [12, 400], asset_mb: 10 },
        })
      if (c.method === 'POST' && c.path === '/templates') return jsonResponse({ id: 'tp1', name: 'Oferta do dia', orientation: 'VERTICAL', version: 1, spec: c.body }, 201)
      return undefined
    })
    renderRoute(<TemplateEditorPage />, '/design/templates/novo', '/design/templates/novo')
    await user.type(await screen.findByLabelText(/^Nome/), 'Oferta do dia')
    await user.click(screen.getByRole('radio', { name: /Vertical 1080×1920/ }))
    await user.click(screen.getByRole('button', { name: 'Variável' }))
    await user.click(screen.getByRole('button', { name: 'Texto' }))
    const text = screen.getByLabelText('Texto')
    await user.clear(text)
    await user.type(text, 'Hoje: {{{{campo1}}')
    await user.click(screen.getAllByRole('button', { name: 'Salvar template' })[0])

    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/templates')).toBe(true))
    const body = calls.find((c) => c.method === 'POST')!.body as { name: string; orientation: string; spec: TemplateSpec; status: string }
    expect(body.name).toBe('Oferta do dia')
    expect(body.orientation).toBe('VERTICAL')
    expect(body.status).toBe('RASCUNHO')
    expect(validateSpec(body.spec)).toEqual([])
    expect(body.spec.variables).toEqual([{ name: 'campo1', label: 'Campo 1', max_len: 80 }])
    const texts = body.spec.layers.filter((l) => l.type === 'text')
    expect(texts[texts.length - 1]).toMatchObject({ type: 'text', text: 'Hoje: {{campo1}}', font: 'dejavu-sans-bold' })
    for (const l of body.spec.layers) {
      expect(l.box.col + l.box.col_span).toBeLessThanOrEqual(24)
      expect(l.box.row + l.box.row_span).toBeLessThanOrEqual(24)
      expect(Object.keys(l)).not.toContain('uid')
    }
  }, 20_000)

  it('variável não declarada bloqueia o salvamento e explica o motivo', async () => {
    const user = userEvent.setup()
    signInAs({ permissions: ['templates.manage'], contexts: [RD_CONTEXT] })
    const { calls } = mockApi((c) =>
      c.path === '/templates/meta'
        ? jsonResponse({ grid: 24, canvases: {}, fonts: ['dejavu-sans', 'dejavu-sans-bold'], layer_types: [], limits: { layers: 30, variables: 20, font_px: [12, 400], asset_mb: 10 } })
        : undefined,
    )
    renderRoute(<TemplateEditorPage />, '/design/templates/novo', '/design/templates/novo')
    await user.type(await screen.findByLabelText(/^Nome/), 'Teste')
    await user.click(screen.getByRole('button', { name: 'Texto' }))
    const text = screen.getByLabelText('Texto')
    await user.clear(text)
    await user.type(text, 'Preço {{{{preco}}')
    expect(await screen.findByText(/variáveis não declaradas: preco/)).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Salvar template' })[0]).toBeDisabled()
    expect(calls.some((c) => c.method === 'POST')).toBe(false)
  }, 20_000)
})
