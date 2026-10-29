/**
 * AÇÃO 004 — etapa "Telas e locais": a RD vê POR QUE telas da seleção ficaram de fora (contagens por motivo,
 * com o próximo passo); o anunciante continua vendo só as contagens públicas (sem mudança).
 */
import { beforeEach, describe, expect, it } from 'vitest'
import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router-dom'
import CampaignWizardPage from '../CampaignWizardPage'
import { jsonResponse, mockFetch, renderWithProviders } from '@/test/utils'
import { availabilityFixture, campaignFixture, signInAs } from './fixtures'

const ZERO = {
  ...availabilityFixture,
  screens_available: 0,
  establishments_count: 0,
  screens_letterbox: 0,
  excluded_by_duration: 0,
  excluded_by_conflict_establishments: 1,
  by_region: [],
  by_state: [],
  by_city: [],
}

const EXCLUSIONS = {
  screens_total: 2,
  out_of_inventory: { screens: 1, establishments: 1, by_reason: [{ reason: 'AWAITING_CONTRACT', screens: 1, establishments: 1 }] },
  duration: { screens: 0 },
  conflict: { screens: 1, establishments: 1, by_reason: [{ reason_code: 'SEGMENTO', screens: 1, establishments: 1 }] },
}

function renderStep(route: string, availability: unknown, establishments: unknown[] = []) {
  mockFetch((url, init) => {
    if (url.includes('/campaigns/availability') && init.method === 'POST') return jsonResponse(availability)
    if (url.includes('/establishments')) return jsonResponse({ items: establishments, total: establishments.length, page: 1, page_size: 20 })
    if (url.endsWith('/campaigns/c1/conflicts')) return jsonResponse({ allowed_screens: 0, blocked_establishments: 0, blocked: [] })
    if (url.endsWith('/campaigns/c1')) return jsonResponse(campaignFixture({ targets: [{ type: 'STATE', value: 'DF', exclude: false }] }))
    return jsonResponse({ items: [], total: 0, page: 1, page_size: 1 })
  })
  return renderWithProviders(
    <Routes>
      <Route path="/anunciante/campanhas/nova" element={<CampaignWizardPage />} />
      <Route path="/campanhas/nova" element={<CampaignWizardPage />} />
    </Routes>,
    { route },
  )
}

describe('disponibilidade — motivo das exclusões (somente RD)', () => {
  beforeEach(() => signInAs('staff'))

  it('RD: aviso em linguagem simples com o motivo e o próximo passo (contrato e conflito de segmento)', async () => {
    renderStep('/campanhas/nova?id=c1&etapa=3', { ...ZERO, exclusions: EXCLUSIONS })
    const panel = await screen.findByTestId('availability')
    const box = await within(panel).findByTestId('availability-exclusions')
    expect(box).toHaveTextContent('visível somente para a RD')
    expect(box).toHaveTextContent('1 tela está fora do inventário: estabelecimento aguardando o aceite do contrato de parceria')
    expect(box).toHaveTextContent('registre o aceite em Estabelecimentos → aba “Contratos e comodato”')
    expect(box).toHaveTextContent('1 local excluído por conflito de segmento (o local vende o mesmo segmento do anúncio)')
    expect(box).toHaveTextContent('ajuste os segmentos do anúncio (etapa 1) ou crie uma exceção em Taxonomia e conflitos → Exceções')
    // o aviso genérico (feito para o anunciante: "os nomes não são exibidos") não se repete para a RD
    expect(within(panel).queryByText(/Os nomes não são exibidos/)).not.toBeInTheDocument()
  })

  it('RD: camada 2 — tela fora do inventário que, depois do aceite, ainda cairia por conflito de segmento', async () => {
    const twoLayers = {
      screens_total: 1,
      out_of_inventory: {
        screens: 1,
        establishments: 1,
        by_reason: [{ reason: 'AWAITING_CONTRACT', screens: 1, establishments: 1 }],
        also_conflict: [{ reason_code: 'SEGMENTO', screens: 1, establishments: 1 }],
      },
      duration: { screens: 0 },
      conflict: { screens: 0, establishments: 0, by_reason: [] },
    }
    renderStep('/campanhas/nova?id=c1&etapa=3', { ...ZERO, excluded_by_conflict_establishments: 0, exclusions: twoLayers })
    const box = await screen.findByTestId('availability-exclusions')
    expect(box).toHaveTextContent('1 tela está fora do inventário: estabelecimento aguardando o aceite do contrato de parceria')
    expect(box).toHaveTextContent(
      'Atenção: mesmo depois de resolver o inventário, 1 tela continuaria fora por conflito de segmento (o local vende o mesmo segmento do anúncio)',
    )
    expect(box).toHaveTextContent('ajuste os segmentos do anúncio (etapa 1)')
  })

  it('tabelas de contagem: cabeçalho "UF"/"Cidade", título acima e nome da cidade com acento', async () => {
    renderStep('/campanhas/nova?id=c1&etapa=3', {
      ...availabilityFixture,
      by_state: [{ state: 'GO', screens: 12 }],
      by_city: [{ city_key: 'GO:goiania', city: 'Goiânia', screens: 12 }],
    })
    await screen.findByTestId('availability')
    const byRegion = screen.getByRole('table', { name: 'Por região' })
    const byUf = screen.getByRole('table', { name: 'Por UF' })
    const byCity = screen.getByRole('table', { name: 'Por cidade' })
    expect(within(byRegion).getByRole('columnheader', { name: 'Região' })).toBeInTheDocument()
    expect(within(byUf).getByRole('columnheader', { name: 'UF' })).toBeInTheDocument()
    expect(within(byCity).getByRole('columnheader', { name: 'Cidade' })).toBeInTheDocument()
    expect(within(byCity).getByText('Goiânia (GO)')).toBeInTheDocument()
    // título da tabela ACIMA dela (caption-top), não no rodapé
    expect(byUf.querySelector('caption')?.className).toContain('caption-top')
  })

  it('seletor de estabelecimento (RD): avisa ao lado do nome que o local está fora do inventário', async () => {
    const user = userEvent.setup()
    renderStep('/campanhas/nova?id=c1&etapa=3', availabilityFixture, [
      { id: 'e9', name: 'CAFOFO BAR', city: 'Samambaia', state: 'DF', is_active: true, onboarding_status: 'AWAITING_CONTRACT' },
      { id: 'e8', name: 'Mercado Vendável', city: 'Goiânia', state: 'GO', is_active: true, onboarding_status: 'ACTIVE' },
    ])
    await screen.findByTestId('availability')
    await user.click(screen.getByRole('combobox', { name: 'Tipo' }))
    await user.click(await screen.findByRole('option', { name: 'Estabelecimento específico' }))
    await user.type(screen.getByRole('searchbox', { name: 'Estabelecimento' }), 'a')
    const list = await screen.findByRole('list', { name: 'Resultados para Estabelecimento' })
    const cafofo = within(list).getByText('CAFOFO BAR').closest('li') as HTMLElement
    expect(cafofo).toHaveTextContent('Fora do inventário: aguardando o aceite do contrato de parceria')
    const ok = within(list).getByText('Mercado Vendável').closest('li') as HTMLElement
    expect(ok).not.toHaveTextContent('Fora do inventário')
    // escolhido: o aviso continua visível
    await user.click(within(cafofo).getByRole('button'))
    expect(await screen.findByText(/Fora do inventário: aguardando o aceite do contrato de parceria/)).toBeInTheDocument()
  })

  it('RD: sem exclusões na seleção ⇒ nenhum aviso extra', async () => {
    const none = { screens_total: 0, out_of_inventory: { screens: 0, establishments: 0, by_reason: [] }, duration: { screens: 0 }, conflict: { screens: 0, establishments: 0, by_reason: [] } }
    renderStep('/campanhas/nova?id=c1&etapa=3', { ...availabilityFixture, excluded_by_conflict_establishments: 0, excluded_by_duration: 0, exclusions: none })
    const panel = await screen.findByTestId('availability')
    expect(within(panel).getByText('12', { selector: 'dd' })).toBeInTheDocument()
    expect(within(panel).queryByTestId('availability-exclusions')).not.toBeInTheDocument()
  })
})

describe('disponibilidade — anunciante sem mudança', () => {
  beforeEach(() => signInAs('advertiser'))

  it('anunciante: só as contagens e o aviso genérico (o servidor não manda o bloco)', async () => {
    renderStep('/anunciante/campanhas/nova?id=c1&etapa=3', ZERO)
    const panel = await screen.findByTestId('availability')
    expect(within(panel).getByText(/1 local\(is\) excluído\(s\) por conflito/)).toBeInTheDocument()
    expect(within(panel).getByText(/Os nomes não são exibidos/)).toBeInTheDocument()
    expect(within(panel).queryByTestId('availability-exclusions')).not.toBeInTheDocument()
    expect(panel).not.toHaveTextContent('fora do inventário')
    expect(panel).not.toHaveTextContent('Contratos e comodato')
  })

  it('anunciante: mesmo que o bloco chegasse, a interface não o exibe (defesa em profundidade)', async () => {
    renderStep('/anunciante/campanhas/nova?id=c1&etapa=3', { ...ZERO, exclusions: EXCLUSIONS })
    const panel = await screen.findByTestId('availability')
    expect(within(panel).queryByTestId('availability-exclusions')).not.toBeInTheDocument()
    expect(panel).not.toHaveTextContent('fora do inventário')
    expect(within(panel).getByText(/Os nomes não são exibidos/)).toBeInTheDocument()
  })
})
