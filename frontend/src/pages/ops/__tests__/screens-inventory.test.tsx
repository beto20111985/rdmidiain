/**
 * AÇÃO 004 (item 12) — "Ativa" é o status de CADASTRO, não elegibilidade. A lista de telas da RD indica se a
 * tela está no inventário vendável e, quando não está, o motivo; o detalhe usa o mesmo texto.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, within } from '@testing-library/react'
import ScreensListPage from '../screens/ScreensListPage'
import ScreenDetailPage from '../screens/ScreenDetailPage'
import { EstablishmentScreens } from '../establishments/tabs/EstablishmentScreens'
import { EstablishmentTaxonomy } from '../establishments/tabs/EstablishmentTaxonomy'
import { renderAt, routeFetch, setMe } from '@/test/opsTestUtils'
import { renderWithProviders } from '@/test/utils'
import type { Establishment, Screen } from '../api/types'

vi.setConfig({ testTimeout: 30_000 })

const BASE: Screen = {
  id: 's1',
  friendly_id: 'TEL-GO-ABCD',
  name: 'Caixa',
  establishment_id: 'e1',
  status: 'ACTIVE',
  sellable: true,
  decommissioned_at: null,
  decommission_reason: null,
  display_type: 'TV',
  player_type: 'TVBOX',
  orientation: 'HORIZONTAL',
  hardware: {
    brand: null,
    model: null,
    inches: 43,
    resolution_w: 1920,
    resolution_h: 1080,
    serial_number: null,
    asset_tag: null,
    player_brand: null,
    player_model: null,
    player_serial_number: null,
    installed_at: null,
    installed_by: null,
  },
  tv_owner: 'ESTABLISHMENT',
  device_id: null,
  screen_class: 'STANDARD',
  weight_milli: 1000,
  programming_mode: 'ESTABLISHMENT_DEFAULT',
  estimated_daily_audience: null,
  notes: null,
  created_at: null,
  updated_at: null,
  establishment: {
    id: 'e1',
    name: 'Mercado Vendável',
    org_id: 'o1',
    city: 'Goiânia',
    state: 'GO',
    city_key: 'GO:goiania',
    timezone: 'America/Sao_Paulo',
    is_active: true,
    onboarding_status: 'ACTIVE',
  },
  equipment: [],
}

const AWAITING: Screen = {
  ...BASE,
  id: 's2',
  friendly_id: 'TEL-DF-Y7ED',
  name: 'TV NOVA',
  establishment_id: 'e2',
  sellable: false,
  establishment: { ...BASE.establishment!, id: 'e2', name: 'Bar Aguardando', city: 'Samambaia', state: 'DF', city_key: 'DF:samambaia', onboarding_status: 'AWAITING_CONTRACT' },
}

const INACTIVE: Screen = { ...BASE, id: 's3', friendly_id: 'TEL-GO-INAT', status: 'INACTIVE', sellable: false }

function presence(ids: string[]) {
  return { items: ids.map((id) => ({ screen_id: id, paired: false, status: 'offline' })), total: ids.length, page: 1, page_size: ids.length }
}

beforeEach(() => setMe(['screens.view', 'screens.manage', 'screens.pair', 'inventory.view', 'establishments.view']))
afterEach(() => vi.unstubAllGlobals())

function rowOf(code: string): HTMLElement {
  const row = screen.getByText(code).closest('tr')
  expect(row).not.toBeNull()
  return row as HTMLElement
}

describe('Telas — "Ativa" não esconde que a tela está fora do inventário', () => {
  it('lista: coluna Inventário com "Vendável" ou "Fora do inventário" + motivo', async () => {
    routeFetch([
      { path: '/screens', reply: () => ({ items: [BASE, AWAITING, INACTIVE], total: 3, page: 1, page_size: 25 }) },
      { path: '/screens/presence', reply: () => presence(['s1', 's2', 's3']) },
    ])
    renderAt(<ScreensListPage />, '/telas', '/telas')
    await screen.findByText('TEL-DF-Y7ED')
    expect(screen.getByRole('columnheader', { name: 'Inventário' })).toBeInTheDocument()

    const ok = rowOf('TEL-GO-ABCD')
    expect(within(ok).getByText('Ativa')).toBeInTheDocument()
    expect(within(ok).getByText('Vendável')).toBeInTheDocument()
    expect(within(ok).queryByText('Fora do inventário')).not.toBeInTheDocument()

    const awaiting = rowOf('TEL-DF-Y7ED')
    expect(within(awaiting).getByText('Ativa')).toBeInTheDocument() // cadastro continua "Ativa"…
    expect(within(awaiting).getByText('Fora do inventário')).toBeInTheDocument() // …mas fora do inventário
    expect(within(awaiting).getByText('Estabelecimento aguardando o aceite do contrato de parceria')).toBeInTheDocument()

    const inactive = rowOf('TEL-GO-INAT')
    expect(within(inactive).getByText('Fora do inventário')).toBeInTheDocument()
    expect(within(inactive).getByText('Tela com status “Inativa”')).toBeInTheDocument()
  })

  it('detalhe: mesmo selo e o mesmo motivo da lista', async () => {
    routeFetch([
      { path: '/screens/s2', reply: () => AWAITING },
      { path: '/screens/s2/presence', reply: () => ({ screen_id: 's2', screen_code: 'TEL-DF-Y7ED', paired: false, device: null, live: null }) },
    ])
    renderAt(<ScreenDetailPage />, '/telas/:id', '/telas/s2')
    expect(await screen.findByText('Fora do inventário')).toBeInTheDocument()
    expect(screen.getByText('Ativa')).toBeInTheDocument()
    expect(screen.getByText(/Fora do inventário: estabelecimento aguardando o aceite do contrato de parceria/)).toBeInTheDocument()
  })

  it('detalhe: tela inativa também explica o motivo', async () => {
    routeFetch([
      { path: '/screens/s3', reply: () => INACTIVE },
      { path: '/screens/s3/presence', reply: () => ({ screen_id: 's3', screen_code: 'TEL-GO-INAT', paired: false, device: null, live: null }) },
    ])
    renderAt(<ScreenDetailPage />, '/telas/:id', '/telas/s3')
    expect(await screen.findByText(/Fora do inventário: tela com status “Inativa”/)).toBeInTheDocument()
  })
})

describe('Estabelecimento — aba Telas e árvore de segmentos', () => {
  it('aba Telas do estabelecimento: mesma coluna Inventário com o motivo', async () => {
    routeFetch([
      { path: '/screens', reply: () => ({ items: [AWAITING], total: 1, page: 1, page_size: 25 }) },
      { path: '/screens/presence', reply: () => presence(['s2']) },
    ])
    const est = { id: 'e2', name: 'Bar Aguardando', is_active: true, onboarding_status: 'AWAITING_CONTRACT' } as unknown as Establishment
    renderWithProviders(<EstablishmentScreens est={est} />)
    await screen.findByText('TEL-DF-Y7ED')
    expect(screen.getByRole('columnheader', { name: 'Inventário' })).toBeInTheDocument()
    const row = rowOf('TEL-DF-Y7ED')
    expect(within(row).getByText('Ativa')).toBeInTheDocument()
    expect(within(row).getByText('Fora do inventário')).toBeInTheDocument()
    expect(within(row).getByText('Estabelecimento aguardando o aceite do contrato de parceria')).toBeInTheDocument()
  })

  it('segmentos: pai antes dos filhos (os subsegmentos não aparecem "dentro" de outro pai)', async () => {
    const seg = (id: string, name: string, depth: number, parent_id: string | null) => ({
      id, name, depth, parent_id, slug: id, is_active: true, ancestor_ids: [], policy: null,
    })
    // ordem do servidor: profundidade, nome (todas as raízes primeiro)
    const items = [
      seg('beb', 'Bebidas', 0, null),
      seg('tab', 'Tabaco', 0, null),
      seg('alc', 'Bebidas alcoólicas', 1, 'beb'),
      seg('nalc', 'Bebidas não alcoólicas', 1, 'beb'),
      seg('cer', 'Cerveja', 2, 'alc'),
    ]
    routeFetch([
      { path: '/taxonomy/segments', reply: () => ({ items, total: items.length, page: 1, page_size: 100 }) },
      { path: '/taxonomy/nodes', reply: () => ({ items: [], total: 0, page: 1, page_size: 100 }) },
      { path: '/taxonomy/products', reply: () => ({ items: [], total: 0, page: 1, page_size: 50 }) },
    ])
    const est = { id: 'e2', name: 'Bar', is_active: true, category_id: null, segment_ids: ['beb', 'cer'], product_ids: [] } as unknown as Establishment
    renderWithProviders(<EstablishmentTaxonomy est={est} />)
    const group = await screen.findByRole('group', { name: 'Segmentos do estabelecimento' })
    await within(group).findByText('Cerveja')
    const order = within(group).getAllByRole('checkbox').map((c) => c.closest('[data-slot="field"]')?.textContent?.trim())
    expect(order).toEqual(['Bebidas', 'Bebidas alcoólicas', 'Cerveja', 'Bebidas não alcoólicas', 'Tabaco'])
  })
})
