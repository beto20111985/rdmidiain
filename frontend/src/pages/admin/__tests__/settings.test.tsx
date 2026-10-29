import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import SettingsPage from '../settings/SettingsPage'
import { describeValue, parseSettingInput } from '../settings/settingValues'
import { apiError, jsonResponse, mockApi, renderRoute, resetSession, signInAs } from '@/test/adminTestUtils'

const POOL = {
  key: 'commission.pool_bps',
  value: 500,
  default: 500,
  type: 'int',
  choices: null,
  min: 0,
  max: 10000,
  financial: true,
  category: 'financeiro',
  description: 'Pool de comissão em basis points (500 = 5%).',
  version: 0,
  updated_at: null,
  updated_by: null,
}
const HOURS = {
  key: 'inventory.reservation_hours',
  value: 2,
  default: 2,
  type: 'int',
  choices: null,
  min: 1,
  max: 48,
  financial: false,
  category: 'inventário',
  description: 'Horas de reserva de inventário antes do pagamento.',
  version: 3,
  updated_at: '2026-10-06T12:00:00Z',
  updated_by: null,
}

// Páginas completas com muitos campos: folga para execução paralela com as demais suítes.
vi.setConfig({ testTimeout: 20_000 })

beforeEach(() => resetSession())
afterEach(() => vi.unstubAllGlobals())

function section(title: RegExp) {
  return screen.getByRole('heading', { name: title }).closest('section') as HTMLElement
}

describe('Configurações — chaves financeiras', () => {
  it('chave financeira mostra o fluxo de DUPLA APROVAÇÃO e cria o pedido SETTING_CHANGE (com Idempotency-Key)', async () => {
    const user = userEvent.setup()
    signInAs({ permissions: ['settings.view', 'settings.manage', 'finance.approve'] })
    const { calls } = mockApi((c) => {
      if (c.path === '/settings') return jsonResponse({ items: [POOL, HOURS] })
      if (c.method === 'POST' && c.path === '/financial/approvals') return jsonResponse({ id: 'ap1', status: 'PENDING' }, 201)
      return undefined
    })
    renderRoute(<SettingsPage />, '/configuracoes', '/configuracoes')
    await screen.findByText(POOL.description)
    const row = section(/Pool de comissão/)
    expect(within(row).getByText(/Financeira · dupla aprovação/)).toBeInTheDocument()
    const btn = within(row).getByRole('button', { name: /Solicitar alteração \(dupla aprovação\)/ })
    expect(btn).toBeDisabled()

    const input = within(row).getByLabelText('Novo valor')
    await user.clear(input)
    await user.type(input, '600')
    await user.type(within(row).getByLabelText(/Motivo do pedido/), 'curto')
    expect(btn).toBeDisabled() // motivo < 10
    await user.type(within(row).getByLabelText(/Motivo do pedido/), ' — ajuste aprovado em reunião')
    await user.click(btn)

    await waitFor(() => expect(calls.some((c) => c.path === '/financial/approvals')).toBe(true))
    const post = calls.find((c) => c.path === '/financial/approvals')!
    expect(post.body).toEqual({
      action_type: 'SETTING_CHANGE',
      payload: { key: 'commission.pool_bps', value: 600, expected_version: 0 },
      reason: 'curto — ajuste aprovado em reunião',
    })
    expect(post.headers['Idempotency-Key']).toMatch(/[0-9a-f-]{36}/)
    // Nunca tenta gravar a chave financeira direto
    expect(calls.some((c) => c.method === 'PUT')).toBe(false)
    expect(await within(row).findByText('Pedido de dupla aprovação criado')).toBeInTheDocument()
    expect(within(row).getByRole('link', { name: /Acompanhar em Aprovações duplas/ })).toHaveAttribute('href', '/financeiro/aprovacoes')
  })

  it('409 SETTING_REQUIRES_DUAL_APPROVAL numa chave não marcada muda a linha para o fluxo de dupla aprovação', async () => {
    const user = userEvent.setup()
    signInAs({ permissions: ['settings.view', 'settings.manage', 'finance.approve'] })
    mockApi((c) => {
      if (c.path === '/settings') return jsonResponse({ items: [HOURS] })
      if (c.method === 'PUT') return apiError(409, 'SETTING_REQUIRES_DUAL_APPROVAL', 'Esta configuração financeira exige dupla aprovação dos Admin Master.')
      return undefined
    })
    renderRoute(<SettingsPage />, '/configuracoes', '/configuracoes')
    await screen.findByText(HOURS.description)
    const row = section(/Horas de reserva/)
    const input = within(row).getByLabelText('Novo valor')
    await user.clear(input)
    await user.type(input, '3')
    await user.click(within(row).getByRole('button', { name: 'Salvar' }))
    expect(await within(row).findByText(/precisa de dupla aprovação dos Admin Master/)).toBeInTheDocument()
    expect(within(row).getByRole('button', { name: /Solicitar alteração \(dupla aprovação\)/ })).toBeInTheDocument()
  })

  it('sem finance.approve a chave financeira fica bloqueada, com a orientação e o link para Aprovações duplas', async () => {
    signInAs({ permissions: ['settings.view', 'settings.manage'] })
    mockApi((c) => (c.path === '/settings' ? jsonResponse({ items: [POOL] }) : undefined))
    renderRoute(<SettingsPage />, '/configuracoes', '/configuracoes')
    await screen.findByText(POOL.description)
    const row = section(/Pool de comissão/)
    expect(within(row).getByText(/Somente os dois Admin Master solicitam e aprovam/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Ver aprovações duplas' })).toHaveAttribute('href', '/financeiro/aprovacoes')
  })

  it('chave comum é salva com PUT, versão otimista e motivo', async () => {
    const user = userEvent.setup()
    signInAs({ permissions: ['settings.view', 'settings.manage'] })
    const { calls } = mockApi((c) => {
      if (c.path === '/settings') return jsonResponse({ items: [HOURS] })
      if (c.method === 'PUT') return jsonResponse({ key: HOURS.key, value: 3, version: 4 })
      return undefined
    })
    renderRoute(<SettingsPage />, '/configuracoes', '/configuracoes')
    await screen.findByText(HOURS.description)
    const row = section(/Horas de reserva/)
    const input = within(row).getByLabelText('Novo valor')
    await user.clear(input)
    await user.type(input, '3')
    await user.click(within(row).getByRole('button', { name: 'Salvar' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    const put = calls.find((c) => c.method === 'PUT')!
    expect(put.path).toBe('/settings/inventory.reservation_hours')
    expect(put.body).toEqual({ value: 3, reason: null, expected_version: 3 })
  })
})

describe('valores de configuração', () => {
  it('valida faixa, inteiros e listas crescentes', () => {
    expect(parseSettingInput({ type: 'int', min: 1, max: 48, choices: null }, '49')).toEqual({ ok: false, error: 'Máximo 48.' })
    expect(parseSettingInput({ type: 'int', min: 1, max: 48, choices: null }, '2.5').ok).toBe(false)
    expect(parseSettingInput({ type: 'list[int]', min: 1, max: 365, choices: null }, '7, 28')).toEqual({ ok: true, value: [7, 28] })
    expect(parseSettingInput({ type: 'list[int]', min: 1, max: 365, choices: null }, '28, 7').ok).toBe(false)
    expect(describeValue('commission.pool_bps', 500)).toMatch(/5,00\s?%/)
    expect(describeValue('design.extra_revision_price_cents', 15000)).toMatch(/R\$\s?150,00/)
    expect(describeValue('player.offline_alert_days', [7, 28])).toBe('7 e 28 dias')
  })
})
