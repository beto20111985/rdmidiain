import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { RealtimeContext, type RealtimeHandler } from '@/lib/realtime/context'
import { TooltipProvider } from '@/components/ui/tooltip'
import { PresenceBadge } from '@/components/ops/presence-badge'
import ScreensListPage from '../screens/ScreensListPage'
import ScreenDetailPage from '../screens/ScreenDetailPage'
import PairingPage from '../screens/PairingPage'
import MyScreenDetailPage from '../unit/MyScreenDetailPage'
import { apiError, callsTo, renderAt, routeFetch, setMe } from '@/test/opsTestUtils'
import type { Screen } from '../api/types'

// Fluxos de várias etapas digitados tecla a tecla: limite explícito por teste (determinístico; o padrão de
// 5 s estoura quando outros agentes usam a CPU do container ao mesmo tempo).
vi.setConfig({ testTimeout: 30_000 })

const SCREEN: Screen = {
  id: 's1',
  friendly_id: 'TEL-SP-ABCD',
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
    brand: 'LG',
    model: 'X',
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
  device_id: 'd1',
  screen_class: 'STANDARD',
  weight_milli: 1000,
  programming_mode: 'ESTABLISHMENT_DEFAULT',
  estimated_daily_audience: null,
  notes: null,
  created_at: null,
  updated_at: null,
  establishment: {
    id: 'e1',
    name: 'Padaria Central',
    org_id: 'o1',
    city: 'Manaus',
    state: 'AM',
    city_key: 'AM:manaus',
    timezone: 'America/Manaus',
    is_active: true,
    onboarding_status: 'ACTIVE',
  },
  equipment: [],
}

const PRESENCE = {
  screen_id: 's1',
  screen_code: 'TEL-SP-ABCD',
  paired: true,
  device: {
    id: 'd1',
    screen_id: 's1',
    platform: 'android',
    app_version: '2.1.0',
    os_version: '11',
    device_info: null,
    update_group: null,
    status: 'ACTIVE',
    key_fingerprint: 'abcdef0123456789abcdef',
    paired_at: '2026-10-01T12:00:00Z',
    pairing_method: 'PANEL',
    revoked_at: null,
    revoked_reason: null,
    presence: 'online',
    player_state: 'PLAYING',
    last_seen_at: null,
    last_sync_at: null,
    last_play_at: null,
    applied_manifest_version: 7,
    pending_manifest_version: null,
    last_error: null,
    telemetry: { cpu_pct: 12, wifi: { ssid: 'Loja', rssi_dbm: -60 } },
    telemetry_at: '2026-10-07T12:00:00Z',
  },
  live: { status: 'reproduzindo', manifest_version_applied: 7, last_seen_ms: Date.parse('2026-10-07T15:00:00Z') },
}

beforeEach(() => setMe(['screens.view', 'screens.manage', 'screens.command', 'screens.pair', 'inventory.view', 'establishments.view']))
afterEach(() => vi.unstubAllGlobals())

describe('PresenceBadge', () => {
  it('offline mostra palavra + último contato no fuso do estabelecimento', () => {
    render(<PresenceBadge presence={{ paired: true, status: 'offline', last_seen_ms: Date.parse('2026-10-07T15:00:00Z') }} timeZone="America/Manaus" />)
    expect(screen.getByText('Offline')).toBeInTheDocument()
    expect(screen.getByText(/Sem comunicação\. Último contato:/)).toHaveTextContent('07/10/2026, 11:00')
  })
  it('não pareada e status desconhecido nunca inventam presença', () => {
    const { rerender } = render(<PresenceBadge presence={{ paired: false, status: 'offline' }} />)
    expect(screen.getByText('Não pareada')).toBeInTheDocument()
    rerender(<PresenceBadge presence={{ paired: true, status: 'xyz' }} />)
    expect(screen.getByText('Sem informação')).toBeInTheDocument()
    rerender(<PresenceBadge presence={undefined} />)
    expect(screen.getByText('—')).toBeInTheDocument()
  })
})

describe('Telas — lista com presença em tempo real', () => {
  it('presença em UM lote (GET /screens/presence) com % de download e evento screen.status sem novo fetch', async () => {
    const handlers = new Map<string, RealtimeHandler>()
    const f = routeFetch([
      { path: '/screens', reply: () => ({ items: [SCREEN, { ...SCREEN, id: 's2', friendly_id: 'TEL-AM-BCDE' }], total: 2, page: 1, page_size: 25 }) },
      {
        path: '/screens/presence',
        // contrato real do W-PLAY; s2 omitida (fora do escopo) → sem presença inventada
        reply: () => ({
          items: [
            {
              screen_id: 's1',
              paired: true,
              device_id: 'd1',
              status: 'reproduzindo',
              player_state: 'PLAYING',
              last_seen_at: '2026-10-07T15:00:00Z',
              app_version: '2.1.0',
              manifest_version_applied: 7,
              manifest_version_pending: 8,
              download: { done_bytes: 30, total_bytes: 120 },
            },
          ],
          total: 1,
          page: 1,
          page_size: 1,
        }),
      },
    ])
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <RealtimeContext.Provider
            value={{
              status: 'open',
              lastSyncAt: null,
              subscribe: (type, h) => {
                handlers.set(type, h)
                return () => handlers.delete(type)
              },
            }}
          >
            <MemoryRouter initialEntries={['/telas']}>
              <Routes>
                <Route path="/telas" element={<ScreensListPage />} />
              </Routes>
            </MemoryRouter>
          </RealtimeContext.Provider>
        </TooltipProvider>
      </QueryClientProvider>,
    )
    expect(await screen.findByText('Reproduzindo')).toBeInTheDocument()
    expect(screen.getByText('2.1.0')).toBeInTheDocument()
    expect(screen.getByText('v7')).toBeInTheDocument()
    expect(screen.getByText('v8 pendente')).toBeInTheDocument()
    expect(screen.getByText('25%')).toBeInTheDocument()
    const batch = callsTo(f, '/screens/presence')
    expect(batch).toHaveLength(1)
    expect(new URL(String(batch[0][0]), 'http://x').searchParams.getAll('screen_id').sort()).toEqual(['s1', 's2'])
    expect(callsTo(f, '/screens/s1/presence')).toHaveLength(0)
    const presenceCalls = callsTo(f, '/presence').length
    act(() => {
      handlers.get('screen.status')?.({
        type: 'screen.status',
        id: 'ev1',
        ts: '2026-10-07T16:00:00Z',
        data: { screen_id: 's1', status: 'offline', last_seen_ms: Date.parse('2026-10-07T16:00:00Z') },
      })
    })
    expect(await screen.findByText('Offline')).toBeInTheDocument()
    expect(screen.getByText(/Sem comunicação/)).toBeInTheDocument()
    expect(callsTo(f, '/presence')).toHaveLength(presenceCalls)
  })
})

describe('Telas — comandos remotos', () => {
  const routes = () => [
    { path: '/screens/s1', reply: () => SCREEN },
    { path: '/screens/s1/presence', reply: () => PRESENCE },
    { path: '/screens/s1/commands', reply: () => ({ items: [], total: 0, page: 1, page_size: 10 }) },
    {
      method: 'POST',
      path: '/screens/s1/commands',
      reply: (_u: URL, init: RequestInit) => ({ id: 'c1', command: JSON.parse(String(init.body)).command, status: 'DELIVERED' }),
    },
    { method: 'POST', path: '/screens/s1/tech-pin', reply: () => ({ id: 'c2', command: 'set_tech_pin', status: 'DELIVERED' }) },
  ]

  it('reiniciar o aparelho exige motivo + código da tela e envia confirm=true com Idempotency-Key', async () => {
    const user = userEvent.setup()
    const f = routeFetch(routes())
    renderAt(<ScreenDetailPage />, '/telas/:id', '/telas/s1?aba=comandos')
    await user.click(await screen.findByRole('button', { name: 'Reiniciar o aparelho na tela TEL-SP-ABCD' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByLabelText(/Motivo/), 'Tela congelada')
    await user.click(within(dialog).getByRole('button', { name: 'Enviar comando' }))
    expect(within(dialog).getByText('O texto digitado não confere.')).toBeInTheDocument()
    await user.type(within(dialog).getByLabelText(/Digite/), 'TEL-SP-ABCD')
    await user.click(within(dialog).getByRole('button', { name: 'Enviar comando' }))
    await waitFor(() => expect(callsTo(f, '/screens/s1/commands', 'POST')).toHaveLength(1))
    const [, init] = callsTo(f, '/screens/s1/commands', 'POST')[0]
    expect(JSON.parse(String(init!.body))).toEqual({ command: 'reboot', reason: 'Tela congelada', confirm: true })
    expect((init!.headers as Record<string, string>)['Idempotency-Key']).toBeTruthy()
  })

  it('PIN técnico: digitado e confirmado, enviado à rota própria (o servidor gera o hash)', async () => {
    const user = userEvent.setup()
    const f = routeFetch(routes())
    renderAt(<ScreenDetailPage />, '/telas/:id', '/telas/s1?aba=comandos')
    await user.click(await screen.findByRole('button', { name: 'Definir PIN técnico da tela TEL-SP-ABCD' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByLabelText(/Novo PIN/), '12a34')
    await user.click(within(dialog).getByRole('button', { name: 'Enviar PIN' }))
    expect(within(dialog).getByText('O PIN deve ter de 6 a 12 dígitos.')).toBeInTheDocument()
    await user.clear(within(dialog).getByLabelText(/Novo PIN/))
    await user.type(within(dialog).getByLabelText(/Novo PIN/), '482913')
    await user.type(within(dialog).getByLabelText(/Confirme o PIN/), '482913')
    await user.type(within(dialog).getByLabelText(/Motivo/), 'Troca de técnico')
    await user.click(within(dialog).getByRole('button', { name: 'Enviar PIN' }))
    await waitFor(() => expect(callsTo(f, '/screens/s1/tech-pin', 'POST')).toHaveLength(1))
    expect(JSON.parse(String(callsTo(f, '/screens/s1/tech-pin', 'POST')[0][1]!.body))).toEqual({ pin: '482913', reason: 'Troca de técnico' })
    expect(callsTo(f, '/screens/s1/commands', 'POST')).toHaveLength(0)
  })

  it('histórico de comandos mostra o autor pelo issued_by_name do servidor (sem /users)', async () => {
    const cmd = {
      id: 'c9',
      device_id: 'd1',
      screen_id: 's1',
      command: 'resync',
      args: null,
      reason: 'Teste',
      confirmed: true,
      issued_by_user_id: 'u5',
      issued_by_name: 'Carlos Técnico',
      status: 'ACKED',
      created_at: '2026-10-07T12:00:00Z',
      expires_at: null,
      delivered_at: null,
      delivery_count: 1,
      acked_at: '2026-10-07T12:00:05Z',
      ack_status: 'OK',
      result_detail: null,
    }
    const f = routeFetch([
      { path: '/screens/s1/commands', reply: () => ({ items: [cmd], total: 1, page: 1, page_size: 10 }) },
      ...routes(),
    ])
    renderAt(<ScreenDetailPage />, '/telas/:id', '/telas/s1?aba=comandos')
    expect(await screen.findByText('Carlos Técnico')).toBeInTheDocument()
    expect(callsTo(f, '/users/u5')).toHaveLength(0)
  })

  it('telemetria não exibe IP e mostra SSID/sinal', async () => {
    routeFetch(routes())
    renderAt(<ScreenDetailPage />, '/telas/:id', '/telas/s1')
    expect(await screen.findByText('Loja')).toBeInTheDocument()
    expect(screen.getByText('-60 dBm')).toBeInTheDocument()
    expect(screen.getByText(/não coleta endereço IP/)).toBeInTheDocument()
  })
})

describe('Parear tela', () => {
  it('lê ?code= do QR, valida e envia o código normalizado para a tela escolhida', async () => {
    const user = userEvent.setup()
    const f = routeFetch([
      { path: '/screens/s1', reply: () => SCREEN },
      { path: '/screens/s1/presence', reply: () => ({ ...PRESENCE, paired: false, device: null, live: { status: 'offline' } }) },
      { path: '/establishments/e1', reply: () => ({ id: 'e1', name: 'Padaria Central', city: 'Manaus', state: 'AM' }) },
      { path: '/establishments', reply: () => ({ items: [], total: 0, page: 1, page_size: 20 }) },
      { path: '/screens', reply: () => ({ items: [SCREEN], total: 1, page: 1, page_size: 20 }) },
      {
        method: 'POST',
        path: '/screens/s1/pairing',
        reply: () => ({ device: PRESENCE.device, revoked_device_ids: [], pairing_request_id: 'r1' }),
      },
    ])
    renderAt(<PairingPage />, '/telas/parear', '/telas/parear?code=abcd2345&screen_id=s1')
    expect(screen.getByLabelText(/Código de pareamento/)).toHaveValue('ABCD-2345')
    await user.click(screen.getByRole('button', { name: 'Confirmar pareamento' }))
    await waitFor(() => expect(callsTo(f, '/screens/s1/pairing', 'POST')).toHaveLength(1))
    expect(JSON.parse(String(callsTo(f, '/screens/s1/pairing', 'POST')[0][1]!.body))).toEqual({ pairing_code: 'ABCD-2345' })
    expect(await screen.findByText('Pareamento confirmado')).toBeInTheDocument()
  })

  it('código inválido não chama o servidor; código expirado mostra orientação', async () => {
    const user = userEvent.setup()
    const f = routeFetch([
      { path: '/screens/s1', reply: () => SCREEN },
      { path: '/screens/s1/presence', reply: () => PRESENCE },
      { path: '/establishments/e1', reply: () => ({ id: 'e1', name: 'Padaria Central', city: 'Manaus', state: 'AM' }) },
      { path: '/establishments', reply: () => ({ items: [], total: 0, page: 1, page_size: 20 }) },
      { path: '/screens', reply: () => ({ items: [SCREEN], total: 1, page: 1, page_size: 20 }) },
      { method: 'POST', path: '/screens/s1/pairing', reply: () => apiError(410, 'PAIRING_CODE_EXPIRED', 'Código expirado.') },
    ])
    renderAt(<PairingPage />, '/telas/parear', '/telas/parear?screen_id=s1')
    await user.type(screen.getByLabelText(/Código de pareamento/), 'ABC0')
    await user.click(screen.getByRole('button', { name: /parear/i }))
    expect(screen.getByText('Digite os 8 caracteres do código exibido na TV.')).toBeInTheDocument()
    expect(callsTo(f, '/pairing', 'POST')).toHaveLength(0)
    expect(await screen.findByText('Esta tela já tem um player pareado')).toBeInTheDocument()
    await user.clear(screen.getByLabelText(/Código de pareamento/))
    await user.type(screen.getByLabelText(/Código de pareamento/), 'ABCD2345')
    await user.click(screen.getByRole('button', { name: 'Substituir player e parear' }))
    expect(await screen.findByText('Código expirado')).toBeInTheDocument()
  })
})

describe('Minha unidade — tela (sem dados comerciais do anunciante)', () => {
  it('mostra só código RD e tipo, nunca chama a grade RD (/grid)', async () => {
    setMe(['screens.view', 'playlists.view', 'playlists.reorder'])
    const f = routeFetch([
      { path: '/screens/s1', reply: () => SCREEN },
      { path: '/screens/s1/presence', reply: () => PRESENCE },
      {
        path: '/scheduler/screens/s1/programming',
        reply: () => ({
          screen_id: 's1',
          screen_code: 'TEL-SP-ABCD',
          timezone: 'America/Manaus',
          rotation: { internal_ms: 120000, external_ms: 120000, cycle_ms: 240000, unit_ms: 30000 },
          anchor_ms: Date.parse('2026-10-07T15:00:00Z'),
          from_ms: Date.parse('2026-10-07T15:00:00Z'),
          to_ms: Date.parse('2026-10-07T21:00:00Z'),
          blocks: [
            {
              bucket_ms: Date.parse('2026-10-07T15:00:00Z'),
              entries: [{ code: 'RD-ABC234', kind: 'EXTERNAL', campaign_id: 'c1', advertiser_name: 'Mercado Concorrente' }],
            },
          ],
          internal_pattern: [[{ code: 'RD-INT222', name: 'Cardápio da casa', duration_ms: 60000 }]],
        }),
      },
    ])
    renderAt(<MyScreenDetailPage />, '/unidade/telas/:id', '/unidade/telas/s1')
    expect(await screen.findByText(/RD-ABC234 · Anúncio/)).toBeInTheDocument()
    // bloco externo começa em âncora + interno (2 min) → 11:02 no fuso de Manaus
    expect(screen.getByText('Anúncios às 11:02')).toBeInTheDocument()
    expect(screen.getByText(/Cardápio da casa · 1:00/)).toBeInTheDocument()
    expect(screen.queryByText(/Mercado Concorrente/)).toBeNull()
    expect(document.body.textContent).not.toContain('c1')
    expect(callsTo(f, '/grid')).toHaveLength(0)
  })

  it('403 na programação → "sem permissão" (sem prévia inventada)', async () => {
    setMe(['screens.view'])
    routeFetch([
      { path: '/screens/s1', reply: () => SCREEN },
      { path: '/screens/s1/presence', reply: () => PRESENCE },
      { path: '/scheduler/screens/s1/programming', reply: () => apiError(403, 'FORBIDDEN', 'Sem permissão.') },
    ])
    renderAt(<MyScreenDetailPage />, '/unidade/telas/:id', '/unidade/telas/s1')
    expect(await screen.findByText('Programação indisponível para o seu perfil')).toBeInTheDocument()
    expect(screen.queryByText(/RD-/)).toBeNull()
  })
})
