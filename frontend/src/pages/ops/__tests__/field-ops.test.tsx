import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import InstallationDetailPage from '../installations/InstallationDetailPage'
import EquipmentDetailPage from '../equipment/EquipmentDetailPage'
import OnboardingApplicationPage from '../onboarding/OnboardingApplicationPage'
import MaintenanceListPage from '../maintenance/MaintenanceListPage'
import MaintenanceDetailPage from '../maintenance/MaintenanceDetailPage'
import { TechnicianSelect } from '@/components/ops/pickers'
import { UserName } from '@/components/ops/entity-name'
import { renderWithProviders } from '@/test/utils'
import { APP_ROUTES } from '@/app/routes'
import { isKnownPermission } from '@/lib/permissions'
import { apiError, callsTo, renderAt, routeFetch, setMe } from '@/test/opsTestUtils'
import type { EquipmentItem, Installation, MaintenanceOrder } from '../api/types'

// Fluxos de várias etapas digitados tecla a tecla: limite explícito por teste (determinístico; o padrão de
// 5 s estoura quando outros agentes usam a CPU do container ao mesmo tempo).
vi.setConfig({ testTimeout: 30_000 })

afterEach(() => vi.unstubAllGlobals())

const INST: Installation = {
  id: 'i1',
  establishment_id: 'e1',
  screen_id: 's1',
  technician_user_id: 'u1',
  status: 'IN_PROGRESS',
  scheduled_date: '2026-10-07',
  started_at: '2026-10-07T12:00:00Z',
  completed_at: null,
  checklist: [{ key: 'tv_fixed', label: 'TV fixada com segurança', required: true, done: false, note: null }],
  geolocation: null,
  connection_test: null,
  connection_test_waiver: null,
  signer: null,
  notes: null,
  created_at: null,
  photos: [],
  establishment_name: 'Padaria Central',
  screen_code: 'TEL-SP-ABCD',
  technician_name: 'Técnico Teste',
}

describe('Instalação (técnico)', () => {
  beforeEach(() => setMe(['equipment.view', 'equipment.manage', 'screens.view']))

  const routes = () => [
    { path: '/installations/i1', reply: () => INST },
    { path: '/screens/s1/presence', reply: () => ({ screen_id: 's1', paired: true, device: null, live: { status: 'online' } }) },
    { method: 'PUT', path: '/installations/i1/geolocation', reply: () => ({ ...INST, geolocation: { lat: -22.9, lng: -47.06, accuracy_m: 8, captured_at: '2026-10-07T12:10:00Z', distance_to_establishment_m: 15 } }) },
    {
      method: 'POST',
      path: '/installations/i1/complete',
      reply: () => apiError(400, 'INSTALLATION_INCOMPLETE', 'Instalação incompleta.', { missing: ['checklist:tv_fixed', 'photo:TV_WORKING', 'signature', 'connection_test'] }),
    },
  ]

  it('GPS só é pedido ao navegador por ação explícita e vai para PUT /geolocation', async () => {
    const user = userEvent.setup()
    const getCurrentPosition = vi.fn((ok: PositionCallback) =>
      ok({ coords: { latitude: -22.9, longitude: -47.06, accuracy: 8.4 } } as GeolocationPosition),
    )
    vi.stubGlobal('navigator', { ...navigator, geolocation: { getCurrentPosition } })
    const f = routeFetch(routes())
    renderAt(<InstallationDetailPage />, '/estoque/instalacoes/:id', '/estoque/instalacoes/i1')
    expect(await screen.findByText('Ainda não registrada.')).toBeInTheDocument()
    expect(getCurrentPosition).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: /Capturar minha localização/ }))
    await waitFor(() => expect(callsTo(f, '/installations/i1/geolocation', 'PUT')).toHaveLength(1))
    expect(getCurrentPosition).toHaveBeenCalledTimes(1)
    expect(JSON.parse(String(callsTo(f, '/geolocation', 'PUT')[0][1]!.body))).toEqual({ lat: -22.9, lng: -47.06, accuracy_m: 8 })
    expect(await screen.findByText(/15 m do endereço cadastrado/)).toBeInTheDocument()
  })

  it('foto em formato errado é recusada ANTES do envio', async () => {
    const user = userEvent.setup({ applyAccept: false })
    const f = routeFetch(routes())
    renderAt(<InstallationDetailPage />, '/estoque/instalacoes/:id', '/estoque/instalacoes/i1')
    const input = await screen.findByLabelText('Selecionar fotos')
    await user.upload(input, new File(['%PDF-1.4'], 'nota.pdf', { type: 'application/pdf' }))
    expect(await screen.findByText(/Formato não aceito/)).toBeInTheDocument()
    expect(callsTo(f, '/photos', 'POST')).toHaveLength(0)
  })

  it('conclusão incompleta lista o que falta (sem inventar sucesso)', async () => {
    const user = userEvent.setup()
    routeFetch(routes())
    renderAt(<InstallationDetailPage />, '/estoque/instalacoes/:id', '/estoque/instalacoes/i1')
    await user.click(await screen.findByRole('button', { name: /Concluir instalação/ }))
    const box = (await screen.findByText('Faltam itens para concluir')).closest('[role="alert"]') as HTMLElement
    expect(within(box).getByText('Foto da TV funcionando')).toBeInTheDocument()
    expect(within(box).getByText('Assinatura do responsável')).toBeInTheDocument()
    expect(within(box).getByText(/Teste de conexão ONLINE recente/)).toBeInTheDocument()
    expect(within(box).getByText(/Checklist obrigatório: 1 item/)).toBeInTheDocument()
  })
})

describe('Estoque — item e movimentação', () => {
  const ITEM: EquipmentItem = {
    id: 'q1',
    type: 'TV',
    tracking: 'SERIAL',
    name: 'TV 43" LG',
    brand: 'LG',
    model: '43UR',
    serial_number: 'SN123',
    asset_tag: 'PAT-1',
    inches: 43,
    unit: 'UN',
    condition: 'NEW',
    supplier: null,
    purchase_date: null,
    purchase_cost_cents: 159990,
    warranty_until: null,
    notes: null,
    status: 'IN_STOCK',
    location: { kind: 'WAREHOUSE', city_key: 'SP:campinas', establishment_id: null, screen_id: null, user_id: null },
    comodato_agreement_id: null,
    expected_return_at: null,
    movement_count: 1,
    last_movement_at: null,
    created_at: null,
    balances: [{ kind: 'WAREHOUSE', ref_id: null, city_key: 'SP:campinas', quantity: 1 }],
    total_quantity: 1,
  }

  it('mostra custo em BRL a partir de centavos e oferece só movimentações válidas a partir do estoque', async () => {
    setMe(['equipment.view', 'equipment.manage'])
    const user = userEvent.setup()
    routeFetch([
      { path: '/equipment/q1', reply: () => ITEM },
      { path: '/equipment/q1/movements', reply: () => ({ items: [], total: 0, page: 1, page_size: 25 }) },
    ])
    renderAt(<EquipmentDetailPage />, '/estoque/:id', '/estoque/q1')
    expect(await screen.findByText(/1\.599,90/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /Movimentar/ }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getAllByRole('combobox')[0])
    const options = (await screen.findAllByRole('option')).map((o) => o.textContent)
    expect(options).toEqual(expect.arrayContaining(['Instalação em tela', 'Empréstimo', 'Comodato', 'Baixa', 'Transferência']))
    expect(options).not.toContain('Devolução')
    expect(options).not.toContain('Ajuste de inventário')
  })
})

describe('Cadastro de parceiro — aprovação', () => {
  it('aprovar exige confirmação; recusar exige motivo', async () => {
    setMe(['establishments.manage', 'users.manage'])
    const user = userEvent.setup()
    const app = {
      id: 'a1',
      origin: 'SELF_SIGNUP',
      status: 'SUBMITTED',
      email: 'dono@exemplo.invalid',
      steps: [
        { key: 'company', required: true, completed: true, staff_only: false },
        { key: 'establishment', required: true, completed: true, staff_only: false },
        { key: 'address', required: true, completed: true, staff_only: false },
      ],
      data: { establishment: { name: 'Padaria Central' } },
      submitted_at: '2026-10-07T12:00:00Z',
      decided_at: null,
      decision_reason: null,
      org_id: null,
      establishment_id: null,
      created_at: '2026-10-07T11:00:00Z',
      documents: [],
    }
    const f = routeFetch([
      { path: '/onboarding/a1', reply: () => app },
      { path: '/organizations', reply: () => ({ items: [], total: 0, page: 1, page_size: 20 }) },
      { method: 'POST', path: '/onboarding/a1/approve', reply: () => ({ ...app, status: 'APPROVED', establishment_id: 'e9' }) },
      { method: 'POST', path: '/onboarding/a1/reject', reply: () => ({ ...app, status: 'REJECTED' }) },
    ])
    renderAt(<OnboardingApplicationPage />, '/estabelecimentos/cadastros/:id', '/estabelecimentos/cadastros/a1')
    await user.click(await screen.findByRole('button', { name: 'Recusar' }))
    let dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Recusar cadastro' }))
    expect(within(dialog).getByText(/pelo menos 10 caracteres/)).toBeInTheDocument()
    expect(callsTo(f, '/reject', 'POST')).toHaveLength(0)
    await user.click(within(dialog).getByRole('button', { name: 'Cancelar' }))
    await user.click(screen.getByRole('button', { name: 'Aprovar' }))
    dialog = await screen.findByRole('alertdialog')
    await user.click(within(dialog).getByRole('button', { name: 'Aprovar cadastro' }))
    await waitFor(() => expect(callsTo(f, '/onboarding/a1/approve', 'POST')).toHaveLength(1))
    expect(await screen.findByRole('link', { name: 'Abrir estabelecimento' })).toHaveAttribute('href', '/estabelecimentos/e9')
  })
})

describe('Rotas do FE-OPS', () => {
  const MINE = [
    '/estabelecimentos', '/estabelecimentos/novo', '/estabelecimentos/:id', '/estabelecimentos/:id/editar', '/estabelecimentos/cadastros',
    '/estabelecimentos/cadastros/:id', '/estabelecimentos/planos', '/estabelecimentos/comodato', '/contratos', '/contratos/:id',
    '/telas', '/telas/nova', '/telas/:id', '/telas/:id/editar', '/telas/parear', '/conteudo-interno', '/conteudo-interno/emergencia',
    '/estoque', '/estoque/novo', '/estoque/:id', '/estoque/manutencao', '/estoque/manutencao/:id', '/estoque/instalacoes',
    '/estoque/instalacoes/:id', '/unidade/telas', '/unidade/telas/:id', '/unidade/programacao', '/unidade/contrato',
  ]
  it('todas as rotas do escopo existem, têm página (load) e permissão do catálogo', () => {
    for (const p of MINE) {
      const r = APP_ROUTES.find((x) => x.path === p)
      expect(r, p).toBeTruthy()
      expect(r!.load, p).toBeTypeOf('function')
      if (r!.permission) expect(isKnownPermission(r!.permission), p).toBe(true)
    }
  })
  it('rotas da unidade são do contexto Estabelecimento; nenhuma rota OPS é do anunciante', () => {
    for (const p of MINE) {
      const r = APP_ROUTES.find((x) => x.path === p)!
      expect(r.contexts).not.toContain('advertiser')
      if (p.startsWith('/unidade')) expect(r.contexts).toEqual(['establishment'])
    }
  })
  it('o auto-cadastro público não é rota autenticada do painel', () => {
    expect(APP_ROUTES.some((r) => r.path === '/cadastro-parceiro')).toBe(false)
  })
})

describe('Técnicos atribuíveis e nomes resolvidos pelo servidor', () => {
  it('lista técnicos por GET /users/assignable (com establishment_id), sem exigir users.view', async () => {
    setMe(['maintenance.manage'])
    const user = userEvent.setup()
    const f = routeFetch([
      {
        path: '/users/assignable',
        reply: () => ({ items: [{ id: 'u7', name: 'Joana Técnica' }], total: 1, truncated: false, permission: 'maintenance.manage' }),
      },
    ])
    renderWithProviders(<TechnicianSelect permission="maintenance.manage" value={null} onChange={() => undefined} establishmentId="e1" />)
    await waitFor(() => expect(callsTo(f, '/users/assignable')).toHaveLength(1))
    const url = new URL(String(callsTo(f, '/users/assignable')[0][0]), 'http://x')
    expect(url.searchParams.get('permission')).toBe('maintenance.manage')
    expect(url.searchParams.get('establishment_id')).toBe('e1')
    expect(callsTo(f, '/api/v1/users?')).toHaveLength(0)
    await user.click(screen.getByRole('combobox'))
    expect(await screen.findByRole('option', { name: 'Joana Técnica' })).toBeInTheDocument()
    expect(screen.getByText(/Somente quem atende este estabelecimento/)).toBeInTheDocument()
  })

  it('sem a permissão, não consulta e explica', () => {
    setMe(['screens.view'])
    const f = routeFetch([])
    renderWithProviders(<TechnicianSelect permission="maintenance.manage" value={null} onChange={() => undefined} />)
    expect(screen.getByText('Atribuir técnico exige a permissão maintenance.manage.')).toBeInTheDocument()
    expect(callsTo(f, '/users')).toHaveLength(0)
  })

  it('ordens de manutenção usam os nomes da lista (sem consultas extras por id)', async () => {
    setMe(['maintenance.manage'])
    const f = routeFetch([
      {
        path: '/maintenance',
        reply: () => ({
          items: [
            {
              id: 'm1',
              establishment_id: 'e1',
              screen_id: 's1',
              equipment_item_id: null,
              component: 'TV',
              status: 'OPEN',
              priority: 'HIGH',
              description: 'Tela sem imagem desde ontem',
              resolution: null,
              technician_user_id: 'u7',
              opened_by: 'u1',
              opened_at: '2026-10-07T12:00:00Z',
              closed_at: null,
              establishment_name: 'Padaria Central',
              screen_code: 'TEL-SP-ABCD',
              technician_name: 'Joana Técnica',
            },
          ],
          total: 1,
          page: 1,
          page_size: 25,
        }),
      },
    ])
    renderAt(<MaintenanceListPage />, '/estoque/manutencao', '/estoque/manutencao')
    expect(await screen.findByText('Padaria Central')).toBeInTheDocument()
    expect(screen.getByText('TEL-SP-ABCD')).toBeInTheDocument()
    expect(screen.getByText('Joana Técnica')).toBeInTheDocument()
    expect(callsTo(f, '/establishments/')).toHaveLength(0)
    expect(callsTo(f, '/screens/')).toHaveLength(0)
    expect(callsTo(f, '/users/')).toHaveLength(0)
  })
})

describe('Fuso do estabelecimento e autoria nas telas de campo', () => {
  it('instalação mostra os horários no fuso do estabelecimento (America/Manaus), não no do navegador', async () => {
    setMe(['equipment.view', 'equipment.manage', 'screens.view', 'establishments.view'])
    routeFetch([
      { path: '/installations/i1', reply: () => INST },
      { path: '/establishments/e1', reply: () => ({ id: 'e1', name: 'Padaria Central', timezone: 'America/Manaus' }) },
      { path: '/screens/s1/presence', reply: () => ({ screen_id: 's1', paired: true, device: null, live: { status: 'online' } }) },
    ])
    renderAt(<InstallationDetailPage />, '/estoque/instalacoes/:id', '/estoque/instalacoes/i1')
    // started_at 12:00Z → 08:00 em Manaus (UTC−4); em São Paulo seria 09:00
    expect(await screen.findByText('07/10/2026, 08:00')).toBeInTheDocument()
  })

  it('o próprio usuário aparece como "(você)" sem consultar /users', async () => {
    setMe([])
    const f = routeFetch([])
    renderWithProviders(<UserName id="u1" />)
    expect(screen.getByText('Pessoa Teste (você)')).toBeInTheDocument()
    expect(callsTo(f, '/users/')).toHaveLength(0)
  })
})

describe('Instalação — editar agendamento (PATCH /installations/{id}, W-ORG §16)', () => {
  beforeEach(() => setMe(['equipment.view', 'equipment.manage', 'screens.view']))
  const NO_SCREEN: Installation = { ...INST, status: 'SCHEDULED', screen_id: null, screen_code: null, started_at: null }
  const SCREEN_S2 = { id: 's2', friendly_id: 'TEL-SP-NOVA', name: 'Balcão', establishment_id: 'e1', status: 'ACTIVE' }
  const base = (inst: Installation) => [
    { path: '/installations/i1', reply: () => inst },
    { path: '/screens', reply: () => ({ items: [SCREEN_S2], total: 1, page: 1, page_size: 20 }) },
    { path: '/screens/s2', reply: () => SCREEN_S2 },
    { path: '/screens/s1/presence', reply: () => ({ screen_id: 's1', paired: true, device: null, live: { status: 'online' } }) },
    {
      path: '/users/assignable',
      reply: () => ({
        items: [
          { id: 'u1', name: 'Técnico Teste' },
          { id: 'u8', name: 'Outra Técnica' },
        ],
        total: 2,
        truncated: false,
        permission: 'equipment.manage',
      }),
    },
  ]

  it('instalação antiga sem tela: "Vincular tela" envia SÓ {screen_id} e a página mostra a tela vinculada', async () => {
    const user = userEvent.setup()
    const f = routeFetch([
      ...base(NO_SCREEN),
      { method: 'PATCH', path: '/installations/i1', reply: () => ({ ...NO_SCREEN, screen_id: 's2', screen_code: 'TEL-SP-NOVA' }) },
    ])
    renderAt(<InstallationDetailPage />, '/estoque/instalacoes/:id', '/estoque/instalacoes/i1')
    expect(await screen.findByText(/ainda não tem tela vinculada/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Vincular tela' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('button', { name: 'Salvar alterações' })).toBeDisabled()
    await user.click(within(within(dialog).getByRole('group', { name: /^Tela/ })).getByRole('combobox'))
    await user.click(await screen.findByRole('option', { name: /TEL-SP-NOVA/ }))
    await user.click(within(dialog).getByRole('button', { name: 'Salvar alterações' }))
    await waitFor(() => expect(callsTo(f, '/installations/i1', 'PATCH')).toHaveLength(1))
    expect(JSON.parse(String(callsTo(f, '/installations/i1', 'PATCH')[0][1]!.body))).toEqual({ screen_id: 's2' })
    expect(await screen.findByRole('link', { name: 'TEL-SP-NOVA' })).toBeInTheDocument()
    expect(screen.queryByText(/ainda não tem tela vinculada/)).toBeNull()
  })

  it('trocar a tela avisa que o teste de conexão será apagado; técnico fora do escopo mostra o erro do contrato', async () => {
    const user = userEvent.setup()
    const tested: Installation = {
      ...INST,
      connection_test: { status: 'ONLINE', checked_at: '2026-10-07T12:05:00Z', source: 'presence' },
    }
    const f = routeFetch([
      ...base(tested),
      {
        method: 'PATCH',
        path: '/installations/i1',
        reply: () => apiError(400, 'TECHNICIAN_OUT_OF_SCOPE', 'O técnico informado não tem permissão de equipamentos neste estabelecimento.'),
      },
    ])
    renderAt(<InstallationDetailPage />, '/estoque/instalacoes/:id', '/estoque/instalacoes/i1')
    await user.click(await screen.findByRole('button', { name: 'Editar agendamento' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(within(dialog).getByRole('group', { name: /^Tela/ })).getByRole('combobox'))
    await user.click(await screen.findByRole('option', { name: /TEL-SP-NOVA/ }))
    expect(within(dialog).getByText(/apaga o teste de conexão/)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('combobox', { name: /Técnico/ }))
    await user.click(await screen.findByRole('option', { name: 'Outra Técnica' }))
    await user.click(within(dialog).getByRole('button', { name: 'Salvar alterações' }))
    await waitFor(() => expect(callsTo(f, '/installations/i1', 'PATCH')).toHaveLength(1))
    expect(JSON.parse(String(callsTo(f, '/installations/i1', 'PATCH')[0][1]!.body))).toEqual({ screen_id: 's2', technician_user_id: 'u8' })
    expect(await within(dialog).findByText('O técnico escolhido não atende este estabelecimento.')).toBeInTheDocument()
  })

  it('autor das fotos e quem agendou vêm do servidor (sem consultar /users)', async () => {
    const f = routeFetch([
      ...base({
        ...INST,
        created_by: 'u9',
        created_by_name: 'Ana Agendadora',
        photos: [
          {
            id: 'p1',
            kind: 'TV_WORKING',
            content_type: 'image/png',
            size_bytes: 1024,
            sha256: 'x',
            width: 10,
            height: 10,
            uploaded_by: 'u1',
            uploaded_by_name: 'Técnico Teste',
          },
        ],
      }),
      { path: '/installations/i1/photos/p1', reply: () => new Response(new Blob(['x'], { type: 'image/png' })) },
    ])
    renderAt(<InstallationDetailPage />, '/estoque/instalacoes/:id', '/estoque/instalacoes/i1')
    expect(await screen.findByText('Ana Agendadora')).toBeInTheDocument()
    expect(screen.getByText(/· por Técnico Teste/)).toBeInTheDocument()
    expect(callsTo(f, '/users/u9')).toHaveLength(0)
  })
})

describe('Manutenção — nomes resolvidos pelo servidor (W-ORG §16 B2)', () => {
  it('"Aberta por", ator e técnico do histórico usam *_name, sem /users e sem "Por —"', async () => {
    setMe(['maintenance.manage'])
    const order: MaintenanceOrder = {
      id: 'm1',
      establishment_id: 'e1',
      screen_id: null,
      equipment_item_id: null,
      component: 'TV',
      status: 'OPEN',
      priority: 'NORMAL',
      description: 'Imagem piscando',
      resolution: null,
      technician_user_id: 'u7',
      opened_by: 'u9',
      opened_by_name: 'Ana Admin',
      opened_at: '2026-10-07T12:00:00Z',
      closed_at: null,
      establishment_name: 'Padaria Central',
      screen_code: null,
      technician_name: 'Joana Técnica',
      history: [
        {
          id: 'h1',
          event: 'ASSIGNED',
          from_status: null,
          to_status: null,
          technician_user_id: 'u7',
          technician_name: 'Joana Técnica',
          note: null,
          actor_user_id: 'u9',
          actor_name: 'Ana Admin',
          created_at: '2026-10-07T12:01:00Z',
        },
      ],
    }
    const f = routeFetch([
      { path: '/maintenance/m1', reply: () => order },
      { path: '/users/assignable', reply: () => ({ items: [], total: 0, truncated: false, permission: 'maintenance.manage' }) },
    ])
    renderAt(<MaintenanceDetailPage />, '/estoque/manutencao/:id', '/estoque/manutencao/m1')
    expect(await screen.findAllByText('Ana Admin')).toHaveLength(2)
    expect(screen.getAllByText('Joana Técnica').length).toBeGreaterThanOrEqual(2)
    expect(screen.queryByText(/^Por —/)).toBeNull()
    expect(callsTo(f, '/users/u9')).toHaveLength(0)
    expect(callsTo(f, '/users/u7')).toHaveLength(0)
  })
})
