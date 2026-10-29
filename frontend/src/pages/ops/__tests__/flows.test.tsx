import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes, useLocation } from 'react-router-dom'
import { headersOf, renderWithProviders } from '@/test/utils'
import { tokenStore } from '@/lib/http'
import UnitContractPage from '../contracts/UnitContractPage'
import UnitSchedulePage from '../unit/UnitSchedulePage'
import PublicPartnerSignupPage from '../onboarding/PublicPartnerSignupPage'
import EmergencyPage from '../content/EmergencyPage'
import { InternalOrderList } from '@/components/ops/internal-order-list'
import { apiError, callsTo, renderAt, routeFetch, setMe } from '@/test/opsTestUtils'
import type { InternalItem } from '../api/types'

// Fluxos de várias etapas digitados tecla a tecla: limite explícito por teste (determinístico; o padrão de
// 5 s estoura quando outros agentes usam a CPU do container ao mesmo tempo).
vi.setConfig({ testTimeout: 30_000 })

afterEach(() => vi.unstubAllGlobals())

const EST = {
  id: 'e1',
  org_id: 'o1',
  name: 'Padaria Central',
  city: 'Campinas',
  state: 'SP',
  timezone: 'America/Sao_Paulo',
  onboarding_status: 'AWAITING_CONTRACT',
  is_active: true,
}

describe('Minha unidade → Contrato (aceite eletrônico)', () => {
  beforeEach(() => setMe(['establishments.view', 'screens.view']))

  it('exige marcar "li e aceito"; após o aceite mostra versão, data/hora e IP', async () => {
    const user = userEvent.setup()
    const f = routeFetch([
      { path: '/establishments', reply: () => ({ items: [EST], total: 1, page: 1, page_size: 100 }) },
      { path: '/contracts/acceptances', reply: () => ({ items: [], total: 0, page: 1, page_size: 50 }) },
      { path: '/comodato-agreements', reply: () => ({ items: [], total: 0, page: 1, page_size: 20 }) },
      {
        path: '/contracts/templates/current',
        reply: () => ({ id: 't1', kind: 'PARTNERSHIP', version: 3, title: 'Contrato de parceria', status: 'PUBLISHED', body: 'Cláusula 1. <b>texto</b> do contrato.' }),
      },
      {
        method: 'POST',
        path: '/contracts/accept',
        reply: () => ({
          id: 'a1',
          template_id: 't1',
          kind: 'PARTNERSHIP',
          version: 3,
          org_id: 'o1',
          establishment_id: 'e1',
          method: 'ELECTRONIC',
          ip_address: '203.0.113.7',
          accepted_at: '2026-10-07T15:00:00Z',
          pdf: { sha256: 'x', size: 2048 },
          establishment_activated: 'e1',
        }),
      },
    ])
    renderAt(<UnitContractPage />, '/unidade/contrato', '/unidade/contrato')
    // texto do contrato é exibido como TEXTO (sem interpretar HTML)
    expect(await screen.findByText(/Cláusula 1\. <b>texto<\/b> do contrato\./)).toBeInTheDocument()
    const accept = screen.getByRole('button', { name: /Aceitar contrato/ })
    expect(accept).toBeDisabled()
    await user.click(screen.getByRole('checkbox', { name: /Li o contrato de parceria/ }))
    await user.click(accept)
    await waitFor(() => expect(callsTo(f, '/contracts/accept', 'POST')).toHaveLength(1))
    expect(JSON.parse(String(callsTo(f, '/contracts/accept', 'POST')[0][1]!.body))).toEqual({
      template_id: 't1',
      org_id: 'o1',
      establishment_id: 'e1',
      comodato_agreement_id: null,
    })
    const ok = await screen.findByText('Aceite registrado')
    expect(ok.parentElement).toHaveTextContent('versão 3')
    expect(ok.parentElement).toHaveTextContent('07/10/2026, 12:00')
    expect(ok.parentElement).toHaveTextContent('203.0.113.7')
  })

  it('aceite durante impersonation mostra orientação clara', async () => {
    const user = userEvent.setup()
    routeFetch([
      { path: '/establishments', reply: () => ({ items: [EST], total: 1, page: 1, page_size: 100 }) },
      { path: '/contracts/acceptances', reply: () => ({ items: [], total: 0, page: 1, page_size: 50 }) },
      { path: '/comodato-agreements', reply: () => ({ items: [], total: 0, page: 1, page_size: 20 }) },
      { path: '/contracts/templates/current', reply: () => ({ id: 't1', kind: 'PARTNERSHIP', version: 1, title: 'C', status: 'PUBLISHED', body: 'Texto' }) },
      { method: 'POST', path: '/contracts/accept', reply: () => apiError(403, 'IMPERSONATION_FORBIDDEN', 'Ação não permitida durante impersonation.') },
    ])
    renderAt(<UnitContractPage />, '/unidade/contrato', '/unidade/contrato')
    await user.click(await screen.findByRole('checkbox', { name: /Li o contrato/ }))
    await user.click(screen.getByRole('button', { name: /Aceitar contrato/ }))
    expect(await screen.findByText(/acesso assistido \(impersonation\)/)).toBeInTheDocument()
  })
})

describe('Minha unidade → Programação (só ordenar)', () => {
  const items: InternalItem[] = [
    { id: 'i1', establishment_id: 'e1', screen_id: null, creative_id: 'c1', position: 0, active: true, created_at: null, updated_at: null, code: 'RD-AAA222', name: 'Cardápio', duration_seconds: 30 },
    { id: 'i2', establishment_id: 'e1', screen_id: null, creative_id: 'c2', position: 1, active: true, created_at: null, updated_at: null, code: 'RD-BBB333', name: 'Promoção', duration_seconds: 60 },
  ]

  it('sem playlists.reorder não há botões de ordenar nem de remover/adicionar', async () => {
    setMe(['establishments.view', 'playlists.view'])
    routeFetch([
      { path: '/establishments', reply: () => ({ items: [EST], total: 1, page: 1, page_size: 100 }) },
      { path: '/internal-content', reply: () => ({ items, total: 2, page: 1, page_size: 2 }) },
    ])
    renderAt(<UnitSchedulePage />, '/unidade/programacao', '/unidade/programacao')
    expect(await screen.findByText(/Cardápio/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Subir|Descer/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /Remover|Adicionar/ })).toBeNull()
    expect(screen.getByText(/só visualiza a ordem/)).toBeInTheDocument()
  })

  it('com playlists.reorder reordena por teclado e salva a nova ordem', async () => {
    setMe(['establishments.view', 'playlists.view', 'playlists.reorder'])
    const user = userEvent.setup()
    const f = routeFetch([
      { path: '/establishments', reply: () => ({ items: [EST], total: 1, page: 1, page_size: 100 }) },
      { path: '/internal-content', reply: () => ({ items, total: 2, page: 1, page_size: 2 }) },
      { method: 'PUT', path: '/internal-content/order', reply: () => ({ items: [items[1], items[0]] }) },
    ])
    renderAt(<UnitSchedulePage />, '/unidade/programacao', '/unidade/programacao')
    await user.click(await screen.findByRole('button', { name: 'Descer Cardápio' }))
    expect(screen.getByText('RD-AAA222 movido para a posição 2 de 2.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Salvar ordem' }))
    await waitFor(() => expect(callsTo(f, '/internal-content/order', 'PUT')).toHaveLength(1))
    expect(JSON.parse(String(callsTo(f, '/internal-content/order', 'PUT')[0][1]!.body))).toEqual({
      establishment_id: 'e1',
      item_ids: ['i2', 'i1'],
    })
  })

  it('lista vazia explica o que acontece (fallback)', () => {
    renderWithProviders(<InternalOrderList items={[]} canReorder canManage={false} />)
    expect(screen.getByText(/exibe o conteúdo de contingência/)).toBeInTheDocument()
  })
})

function LocationProbe() {
  const loc = useLocation()
  return <p data-testid="url">{loc.pathname + loc.search}</p>
}

describe('Auto-cadastro público (/cadastro-parceiro)', () => {
  it('token do e-mail vai SÓ no header X-RD-Onboarding-Token, sai da URL e não usa Bearer', async () => {
    tokenStore.clear()
    const f = routeFetch([
      {
        path: '/onboarding/public/application',
        reply: () => ({
          id: 'a1',
          origin: 'SELF_SIGNUP',
          status: 'IN_PROGRESS',
          email: 'dono@exemplo.invalid',
          steps: [
            { key: 'company', required: true, completed: false, staff_only: false },
            { key: 'establishment', required: true, completed: false, staff_only: false },
            { key: 'address', required: true, completed: false, staff_only: false },
            { key: 'screens', required: false, completed: false, staff_only: false },
          ],
          data: {},
          documents: [],
        }),
      },
    ])
    renderWithProviders(
      <Routes>
        <Route
          path="/cadastro-parceiro"
          element={
            <>
              <PublicPartnerSignupPage />
              <LocationProbe />
            </>
          }
        />
      </Routes>,
      { route: '/cadastro-parceiro?token=segredo-do-link' },
    )
    expect(await screen.findByRole('heading', { level: 2, name: 'Empresa' })).toBeInTheDocument()
    expect(screen.getByTestId('url')).toHaveTextContent(/^\/cadastro-parceiro$/)
    const [url, init] = callsTo(f, '/onboarding/public/application')[0]
    expect(String(url)).not.toContain('segredo')
    expect(headersOf(init as RequestInit)['X-RD-Onboarding-Token']).toBe('segredo-do-link')
    expect(headersOf(init as RequestInit).Authorization).toBeUndefined()
    // sem categoria/segmentos para o público (taxonomia exige autenticação)
    expect(callsTo(f, '/taxonomy')).toHaveLength(0)
  })

  it('link inválido/expirado → aviso + pedido de novo link (resposta neutra)', async () => {
    const user = userEvent.setup()
    const f = routeFetch([
      { path: '/onboarding/public/application', reply: () => apiError(401, 'ONBOARDING_TOKEN_INVALID', 'Link de cadastro inválido ou expirado.') },
      { method: 'POST', path: '/onboarding/public/signup', reply: () => ({ status: 'accepted', message: 'ok' }) },
    ])
    renderWithProviders(
      <Routes>
        <Route path="/cadastro-parceiro" element={<PublicPartnerSignupPage />} />
      </Routes>,
      { route: '/cadastro-parceiro?token=velho' },
    )
    expect(await screen.findByText('Link inválido ou expirado')).toBeInTheDocument()
    await user.type(screen.getByLabelText(/E-mail do responsável/), 'dono@exemplo.invalid')
    await user.click(screen.getByRole('button', { name: 'Receber link de cadastro' }))
    expect(await screen.findByText('Verifique seu e-mail')).toBeInTheDocument()
    expect(callsTo(f, '/auth/refresh', 'POST')).toHaveLength(0)
  })
})

describe('Comunicado de emergência', () => {
  beforeEach(() => setMe(['campaigns.kill', 'creatives.view', 'screens.view', 'establishments.view']))

  it('exige confirmação forte (EMERGENCIA) e envia a janela em UTC a partir do horário de Brasília', async () => {
    const user = userEvent.setup()
    const f = routeFetch([
      { path: '/scheduler/emergencies', reply: () => ({ items: [] }) },
      {
        path: '/creatives',
        reply: () => ({ items: [{ id: 'cr1', code: 'RD-EMG222', name: 'Evacuação', kind: 'INSTITUTIONAL', status: 'APROVADO', duration_seconds: 30 }], total: 1, page: 1, page_size: 25 }),
      },
      { method: 'POST', path: '/scheduler/emergencies', reply: () => ({ id: 'em1', title: 'Evacuar', screens: 12 }) },
    ])
    renderAt(<EmergencyPage />, '/conteudo-interno/emergencia', '/conteudo-interno/emergencia')
    await user.type(await screen.findByLabelText(/^Título/), 'Evacuar o shopping')
    const sel = await screen.findAllByRole('combobox')
    // seleciona a peça (2º select do picker de criativo)
    await user.click(sel[0])
    await user.click(await screen.findByRole('option', { name: /RD-EMG222/ }))
    const start = screen.getByLabelText(/Início/)
    const end = screen.getByLabelText(/^Fim/)
    fireEvent.change(start, { target: { value: '2099-10-07T14:30' } })
    fireEvent.change(end, { target: { value: '2099-10-07T16:00' } })
    await user.click(screen.getByRole('button', { name: /Revisar e publicar/ }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByLabelText(/Motivo/), 'Alarme de incêndio no prédio')
    await user.type(within(dialog).getByLabelText(/Digite/), 'emergencia')
    await user.click(within(dialog).getByRole('button', { name: 'Publicar comunicado' }))
    expect(within(dialog).getByText('O texto digitado não confere.')).toBeInTheDocument()
    await user.clear(within(dialog).getByLabelText(/Digite/))
    await user.type(within(dialog).getByLabelText(/Digite/), 'EMERGENCIA')
    await user.click(within(dialog).getByRole('button', { name: 'Publicar comunicado' }))
    await waitFor(() => expect(callsTo(f, '/scheduler/emergencies', 'POST')).toHaveLength(1))
    const [, init] = callsTo(f, '/scheduler/emergencies', 'POST')[0]
    expect(JSON.parse(String(init!.body))).toMatchObject({
      title: 'Evacuar o shopping',
      creative_id: 'cr1',
      starts_at: '2099-10-07T17:30:00Z',
      ends_at: '2099-10-07T19:00:00Z',
      target_kind: 'ALL',
      target_ids: [],
      reason: 'Alarme de incêndio no prédio',
    })
    expect(headersOf(init as RequestInit)['Idempotency-Key']).toBeTruthy()
  })
})
