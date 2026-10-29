import { beforeEach, describe, expect, it } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import MediaLibraryPage from '../MediaLibraryPage'
import MarginPage from '../MarginPage'
import SchedulePage from '../SchedulePage'
import { OverridesTab } from '../taxonomy/MatrixTabs'
import { jsonResponse, mockFetch, renderWithProviders } from '@/test/utils'
import { signInAs } from './fixtures'

beforeEach(() => signInAs('staff'))

const readyMedia = {
  id: 'm1',
  org_id: 'org-1',
  name: 'promo-verao.mp4',
  original_filename: 'promo-verao.mp4',
  origin: 'UPLOAD',
  kind: 'VIDEO',
  status: 'READY',
  status_code: null,
  status_message: null,
  progress_pct: 100,
  attempts: 1,
  extension_mismatch: false,
  source: { format: 'webm', mime: 'video/webm', size_bytes: 9000000, sha256: 'b'.repeat(64), width: 3840, height: 2160, duration_ms: 30000, codec: 'vp9', fps_milli: 60000, has_audio: true },
  output: { mime_type: 'video/mp4', size_bytes: 4000000, sha256: 'c'.repeat(64), width: 1920, height: 1080, duration_ms: 30000, codec: 'h264', fps_milli: 30000, orientation: 'HORIZONTAL', animated: false, frame_count: null, has_audio: false },
  has_preview: false,
  content_url: '/api/v1/media/m1/content',
  preview_url: null,
  created_by: 'u1',
  created_at: '2099-01-01T12:00:00Z',
  updated_at: '2099-01-01T12:00:00Z',
  ready_at: '2099-01-01T12:05:00Z',
}

describe('biblioteca de mídia', () => {
  it('metadados reais e exclusão bloqueada quando a mídia está em uso (com o motivo)', async () => {
    mockFetch((url) => {
      if (url.includes('/media/m1/usage')) return jsonResponse({ media_id: 'm1', in_use: true, references: { creatives: 2 } })
      if (url.endsWith('/media/m1')) return jsonResponse(readyMedia)
      if (url.includes('/media?')) return jsonResponse({ items: [readyMedia], total: 1, page: 1, page_size: 25 })
      if (url.includes('/organizations/')) return jsonResponse({ id: 'org-1', name: 'Loja Teste' })
      return jsonResponse({})
    })
    const user = userEvent.setup()
    renderWithProviders(<MediaLibraryPage />, { route: '/biblioteca' })
    await user.click(await screen.findByRole('button', { name: 'promo-verao.mp4' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('1920 × 1080')).toBeInTheDocument()
    expect(within(dialog).getByText('h264')).toBeInTheDocument()
    expect(within(dialog).getByText('Removido no processamento')).toBeInTheDocument()
    expect(await within(dialog).findByText(/Esta mídia está em uso \(2 anúncio\(s\)\)/)).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Excluir' })).toBeDisabled()
  })
})

describe('margem', () => {
  it('margem negativa calculada pelo servidor aparece como bloqueio, com todos os componentes', async () => {
    const margin = {
      revenue_cents: 100000,
      media_revenue_cents: 100000,
      gateway_cents: 1000,
      establishment_commission_cents: 5000,
      seller_commission_cents: 0,
      taxes_cents: 15000,
      server_cents: 30000,
      equipment_cents: 60000,
      designer_cents: 0,
      direct_costs_cents: 0,
      total_costs_cents: 111000,
      margin_cents: -11000,
      margin_bps: -1100,
      min_margin_bps: 4000,
      commission_pool_bps: 500,
      status: 'NEGATIVE',
    }
    mockFetch((url) => {
      if (url.endsWith('/pricing/margin-simulator'))
        return jsonResponse({ quote: { lines: [], media_gross_cents: 100000, discount_cents: 0, min_price_adjustment_cents: 0, media_cents: 100000, fees_cents: 0, total_cents: 100000, margin }, margin })
      if (url.endsWith('/instance/price-floors')) return jsonResponse({ items: [] })
      return jsonResponse({})
    })
    const user = userEvent.setup()
    renderWithProviders(<MarginPage />, { route: '/precos/margem' })
    await user.click(screen.getByRole('button', { name: 'Calcular margem' }))
    expect(await screen.findByText('Bloqueado: margem negativa')).toBeInTheDocument()
    for (const label of ['Gateway de pagamento', 'Comissão dos estabelecimentos (pool) (5,00%)', 'Comissão do vendedor', 'Impostos', 'Servidor', 'Equipamento', 'Designer', 'Custos diretos'])
      expect(screen.getByText(label)).toBeInTheDocument()
    expect(screen.getByTestId('margin-value')).toHaveTextContent('-R$ 110,00')
  })
})

describe('override campanha × estabelecimento', () => {
  it('exige justificativa de 20 caracteres antes de chamar o servidor', async () => {
    const fetchMock = mockFetch((url) => {
      if (url.includes('/conflicts/overrides')) return jsonResponse({ items: [] })
      if (url.includes('/campaigns?')) return jsonResponse({ items: [{ id: 'c1', name: 'Campanha X', status: 'ATIVA' }], total: 1, page: 1, page_size: 20 })
      if (url.includes('/establishments?')) return jsonResponse({ items: [{ id: 'e1', name: 'Mercado Central', city: 'Goiânia', state: 'GO' }], total: 1, page: 1, page_size: 20 })
      return jsonResponse({})
    })
    const user = userEvent.setup()
    renderWithProviders(<OverridesTab canOverride />, { route: '/taxonomia?aba=overrides' })
    await user.click(await screen.findByRole('button', { name: /Campanha X/ }))
    await user.click(await screen.findByRole('button', { name: /Mercado Central/ }))
    await user.type(screen.getByRole('textbox', { name: /Justificativa/ }), 'muito curta')
    await user.click(screen.getByRole('button', { name: 'Registrar override' }))
    expect(await screen.findByText('A justificativa precisa ter ao menos 20 caracteres.')).toBeInTheDocument()
    await waitFor(() => expect(fetchMock.mock.calls.some(([u, i]) => String(u).endsWith('/conflicts/overrides') && (i as RequestInit).method === 'POST')).toBe(false))
  })
})

describe('grade de agendamentos', () => {
  it('duração das entradas vem em milissegundos (RDPP/1 d_ms) e aparece em segundos, no fuso da tela', async () => {
    mockFetch((url) => {
      if (url.includes('/scheduler/screens/s1/grid'))
        return jsonResponse({
          screen_id: 's1',
          timezone: 'America/Manaus',
          rotation: { cycle_ms: 240000 },
          anchor_ms: 0,
          horizon: null,
          internal_pattern: [],
          fallback: [],
          emergency: [],
          revoked: [],
          blocks: [
            {
              bucket_ms: Date.parse('2026-10-08T11:36:00Z'),
              entries: [
                { u: 0, d: 30000, code: 'RD-INT001', kind: 'INTERNAL', campaign_id: null, creative_id: 'cr1' },
                { u: 1, d: 60000, code: 'RD-EXT002', kind: 'EXTERNAL', campaign_id: 'c1', creative_id: 'cr2' },
              ],
            },
          ],
          unplaced: [],
          plan_sha256: 'abc',
        })
      return jsonResponse({ items: [] })
    })
    renderWithProviders(<SchedulePage />, { route: '/programacao?tela=s1&codigo=TEL-AM-TEST' })
    expect(await screen.findByText('RD-INT001 · Interno · 30 s')).toBeInTheDocument()
    expect(screen.getByText('RD-EXT002 · Anúncio · 60 s')).toBeInTheDocument()
    // 11:36 UTC = 07:36 em Manaus (hora da tela)
    expect(screen.getByText('08/10/2026, 07:36')).toBeInTheDocument()
  })
})
