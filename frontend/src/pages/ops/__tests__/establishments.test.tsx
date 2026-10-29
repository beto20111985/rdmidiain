import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import EstablishmentsListPage from '../establishments/EstablishmentsListPage'
import EstablishmentCreatePage from '../establishments/EstablishmentCreatePage'
import EstablishmentDetailPage from '../establishments/EstablishmentDetailPage'
import { apiError, callsTo, renderAt, routeFetch, setMe } from '@/test/opsTestUtils'
import type { Establishment } from '../api/types'

// Fluxos de várias etapas digitados tecla a tecla: limite explícito por teste (determinístico; o padrão de
// 5 s estoura quando outros agentes usam a CPU do container ao mesmo tempo).
vi.setConfig({ testTimeout: 30_000 })

const EST: Establishment = {
  id: 'e1',
  org_id: 'o1',
  name: 'Padaria Central',
  trade_name: 'Pão Quente',
  legal_name: null,
  document: null,
  document_type: null,
  responsible_name: 'Ana',
  phone: null,
  email: null,
  address: 'Rua A',
  number: '10',
  complement: null,
  neighborhood: 'Centro',
  city: 'Campinas',
  city_key: 'SP:campinas',
  city_ibge_code: null,
  state: 'SP',
  region: 'SUDESTE',
  region_label: 'Sudeste',
  country: 'BRA',
  postal_code: null,
  lat: -22.9,
  lng: -47.06,
  geo_source: 'MANUAL',
  geo_accuracy_m: null,
  geo_captured_at: null,
  timezone: 'America/Sao_Paulo',
  timezone_source: 'AUTO',
  category_id: null,
  venue_class: 'STANDARD',
  weight_milli: 1000,
  estimated_daily_visitors: { value: 800, is_estimate: true, method: 'MANUAL', label: 'Audiência estimada' },
  partner_plan_id: 'p1',
  receives_commission: true,
  pays_monthly_fee: false,
  monthly_fee_cents: null,
  onboarding_status: 'AWAITING_CONTRACT',
  responsible_technician_id: null,
  installation_date: null,
  is_active: true,
  deactivated_at: null,
  deactivation_reason: null,
  operational_config: null,
  notes: null,
  created_at: '2026-10-01T12:00:00Z',
  updated_at: null,
  screens_count: 2,
  segment_ids: [],
  product_ids: [],
}

const page = (items: Establishment[]) => ({ items, total: items.length, page: 1, page_size: 25 })
const catalogs = [
  { path: '/taxonomy/nodes', reply: () => page([]) },
  { path: '/taxonomy/segments', reply: () => page([]) },
  { path: '/partner-plans', reply: () => ({ items: [{ id: 'p1', name: 'Plano Gratuito' }], total: 1, page: 1, page_size: 1 }) },
]

beforeEach(() => setMe(['establishments.view', 'establishments.manage', 'audit.view']))
afterEach(() => vi.unstubAllGlobals())

describe('Estabelecimentos — lista', () => {
  it('lista do servidor com busca → `search`, UF + cidade → `city_key`, plano e contrato rotulados', async () => {
    const f = routeFetch([...catalogs, { path: '/establishments', reply: () => page([EST]) }])
    renderAt(<EstablishmentsListPage />, '/estabelecimentos', '/estabelecimentos?q=pad&state=SP&city=Campinas')
    expect(await screen.findByRole('link', { name: 'Padaria Central' })).toHaveAttribute('href', '/estabelecimentos/e1')
    const url = new URL(String(callsTo(f, '/api/v1/establishments')[0][0]), 'http://x')
    expect(url.searchParams.get('search')).toBe('pad')
    expect(url.searchParams.get('city_key')).toBe('SP:Campinas')
    expect(url.searchParams.get('q')).toBeNull()
    expect(await screen.findByText('Plano Gratuito')).toBeInTheDocument()
    expect(screen.getByText('Aguardando aceite do contrato')).toBeInTheDocument()
  })

  it('erro do envelope aparece com "Tentar novamente" (filtros preservados)', async () => {
    routeFetch([...catalogs, { path: '/establishments', reply: () => apiError(500, 'INTERNAL', 'Falha temporária no servidor.') }])
    renderAt(<EstablishmentsListPage />, '/estabelecimentos', '/estabelecimentos?state=RJ')
    expect(await screen.findByText('Falha temporária no servidor.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Tentar novamente/ })).toBeInTheDocument()
  })

  it('403 → estado "sem permissão"', async () => {
    routeFetch([...catalogs, { path: '/establishments', reply: () => apiError(403, 'FORBIDDEN', 'Sem permissão.') }])
    renderAt(<EstablishmentsListPage />, '/estabelecimentos', '/estabelecimentos')
    expect(await screen.findByText('Você não tem acesso a esta área')).toBeInTheDocument()
  })

  it('filtro de plano do parceiro vai ao servidor como `partner_plan_id`', async () => {
    const f = routeFetch([...catalogs, { path: '/establishments', reply: () => page([EST]) }])
    renderAt(<EstablishmentsListPage />, '/estabelecimentos', '/estabelecimentos?partner_plan_id=p1&region=SUDESTE')
    expect(await screen.findByRole('link', { name: 'Padaria Central' })).toBeInTheDocument()
    const url = new URL(String(callsTo(f, '/api/v1/establishments')[0][0]), 'http://x')
    expect(url.searchParams.get('partner_plan_id')).toBe('p1')
    expect(url.searchParams.get('region')).toBe('SUDESTE')
    expect(screen.queryByText(/ainda não aplica/)).toBeNull()
  })

  it('sem establishments.manage não oferece "Adicionar estabelecimento"', async () => {
    setMe(['establishments.view'])
    routeFetch([...catalogs, { path: '/establishments', reply: () => page([]) }])
    renderAt(<EstablishmentsListPage />, '/estabelecimentos', '/estabelecimentos')
    expect(await screen.findByText('Nenhum estabelecimento cadastrado no seu acesso.')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Adicionar estabelecimento/ })).toBeNull()
  })
})

describe('Estabelecimentos — formulário', () => {
  it('valida obrigatórios junto aos campos sem chamar a API', async () => {
    const user = userEvent.setup()
    const f = routeFetch([...catalogs, { path: '/organizations', reply: () => page([]) }])
    renderAt(<EstablishmentCreatePage />, '/estabelecimentos/novo', '/estabelecimentos/novo')
    await user.click(screen.getByRole('button', { name: 'Cadastrar estabelecimento' }))
    expect(await screen.findByText('Selecione a empresa.')).toBeInTheDocument()
    expect(screen.getByText('Informe o nome (mínimo 2 caracteres).')).toBeInTheDocument()
    expect(screen.getByText('Selecione a UF.')).toBeInTheDocument()
    expect(screen.getByLabelText(/Nome do estabelecimento/)).toHaveAttribute('aria-invalid', 'true')
    expect(callsTo(f, '/api/v1/establishments', 'POST')).toHaveLength(0)
  })
})

describe('Estabelecimentos — detalhe', () => {
  it('mostra audiência como ESTIMADA com método e alerta de contrato pendente', async () => {
    const user = userEvent.setup()
    routeFetch([
      ...catalogs,
      { path: '/establishments/e1', reply: () => EST },
      { path: '/establishments/timezone-suggestion', reply: () => ({ timezone: 'America/Sao_Paulo', reason: 'Fuso padrão da UF.', confidence: 'STATE' }) },
      { path: '/establishments/e1/audience-estimates', reply: () => page([]) },
    ])
    renderAt(<EstablishmentDetailPage />, '/estabelecimentos/:id', '/estabelecimentos/e1')
    expect(await screen.findByText('Aguardando o aceite do contrato de parceria')).toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: 'Audiência estimada' }))
    const card = (await screen.findByText('Audiência atual')).closest('[data-slot="card"]') as HTMLElement
    expect(within(card).getByText('ESTIMADA')).toBeInTheDocument()
    expect(within(card).getByText(/Informada manualmente/)).toBeInTheDocument()
    expect(within(card).getByText(/não é medição/)).toBeInTheDocument()
  })

  it('inativar exige motivo e o nome digitado', async () => {
    const user = userEvent.setup()
    const f = routeFetch([
      ...catalogs,
      { path: '/establishments/e1', reply: () => EST },
      { path: '/establishments/timezone-suggestion', reply: () => ({ timezone: 'America/Sao_Paulo', reason: 'x', confidence: 'STATE' }) },
      { method: 'POST', path: '/establishments/e1/deactivate', reply: () => ({ ...EST, is_active: false }) },
    ])
    renderAt(<EstablishmentDetailPage />, '/estabelecimentos/:id', '/estabelecimentos/e1')
    await user.click(await screen.findByRole('button', { name: 'Inativar' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Inativar estabelecimento' }))
    expect(await within(dialog).findByText(/pelo menos 10 caracteres/)).toBeInTheDocument()
    await user.type(within(dialog).getByLabelText(/Motivo/), 'Encerrou as atividades no local.')
    await user.type(within(dialog).getByLabelText(/Digite/), 'Padaria')
    await user.click(within(dialog).getByRole('button', { name: 'Inativar estabelecimento' }))
    expect(within(dialog).getByText('O texto digitado não confere.')).toBeInTheDocument()
    expect(callsTo(f, '/deactivate', 'POST')).toHaveLength(0)
    await user.clear(within(dialog).getByLabelText(/Digite/))
    await user.type(within(dialog).getByLabelText(/Digite/), 'Padaria Central')
    await user.click(within(dialog).getByRole('button', { name: 'Inativar estabelecimento' }))
    await waitFor(() => expect(callsTo(f, '/deactivate', 'POST')).toHaveLength(1))
    expect(JSON.parse(String(callsTo(f, '/deactivate', 'POST')[0][1]!.body))).toEqual({ reason: 'Encerrou as atividades no local.' })
  })
})
