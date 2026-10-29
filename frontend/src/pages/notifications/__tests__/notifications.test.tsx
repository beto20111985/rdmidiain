import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router-dom'
import NotificationsPage from '../NotificationsPage'
import { NotificationBell } from '@/components/layout/notification-bell'
import { renderWithProviders } from '@/test/utils'
import { jsonResponse, mockApi, resetSession, signInAs, withPageMeta } from '@/test/adminTestUtils'

const N1 = {
  id: 'n1',
  event_type: 'payment.approved',
  title: 'Pagamento aprovado',
  body: 'O pagamento da campanha Verão foi confirmado.',
  data: {},
  link: '/anunciante/faturas',
  entity_type: 'payment',
  entity_id: 'p1',
  read: false,
  read_at: null,
  created_at: '2026-10-07T12:00:00Z',
  deliveries: [
    { channel: 'EMAIL', status: 'SENT', detail: null },
    { channel: 'PUSH', status: 'UNAVAILABLE', detail: 'push: indisponível (dependência externa)' },
  ],
}

// Páginas completas com muitos campos: folga para execução paralela com as demais suítes.
vi.setConfig({ testTimeout: 20_000 })

beforeEach(() => resetSession())
afterEach(() => vi.unstubAllGlobals())

function renderPage(url = '/notificacoes') {
  return renderWithProviders(
    withPageMeta(
      <>
        <NotificationBell />
        <Routes>
          <Route path="/notificacoes" element={<NotificationsPage />} />
        </Routes>
      </>,
    ),
    { route: url },
  )
}

describe('Notificações — contador real e central', () => {
  it('o sino usa GET /notifications/unread-count e marcar como lida atualiza o contador real', async () => {
    const user = userEvent.setup()
    signInAs({ permissions: ['campaigns.view'] })
    let unread = 1
    const { calls } = mockApi((c) => {
      if (c.path === '/notifications/unread-count') return jsonResponse({ count: unread })
      if (c.method === 'GET' && c.path.startsWith('/notifications?'))
        return jsonResponse({ items: [{ ...N1, read: unread === 0, read_at: unread === 0 ? '2026-10-07T12:05:00Z' : null }], total: 1, page: 1, page_size: 25 })
      if (c.method === 'POST' && c.path === '/notifications/n1/read') {
        unread = 0
        return new Response(null, { status: 204 })
      }
      return undefined
    })
    renderPage()
    expect(await screen.findByRole('link', { name: 'Notificações, 1 não lidas' })).toHaveAttribute('href', '/notificacoes')
    expect(await screen.findByText('Pagamento aprovado')).toBeInTheDocument()
    // Push nunca aparece como enviado
    expect(screen.getByText('indisponível')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /Marcar “Pagamento aprovado” como lida/ }))
    await waitFor(() => expect(screen.getByRole('link', { name: 'Notificações, nenhuma não lida' })).toBeInTheDocument())
    expect(calls.filter((c) => c.path === '/notifications/unread-count').length).toBeGreaterThanOrEqual(2)
    expect(calls.some((c) => /refetchInterval|interval/.test(c.path))).toBe(false)
  })

  it('preferências: push aparece indisponível quando o servidor diz e não pode ser ligado', async () => {
    const user = userEvent.setup()
    signInAs({ permissions: ['campaigns.view'] })
    let emailOn = true
    const prefs = () => ({
          items: [
            {
              event_type: 'campaign.approved',
              label: 'Campanha aprovada',
              critical: false,
              channels: [
                { channel: 'IN_APP', enabled: true, locked: false, default_channel: true, available: true, unavailable_reason: null },
                { channel: 'EMAIL', enabled: emailOn, locked: false, default_channel: true, available: true, unavailable_reason: null },
                { channel: 'PUSH', enabled: true, locked: false, default_channel: false, available: false, unavailable_reason: 'push: indisponível (dependência externa — provedor não configurado)' },
              ],
            },
          ],
        })
    const { calls } = mockApi((c) => {
      if (c.path === '/notifications/unread-count') return jsonResponse({ count: 0 })
      if (c.method === 'GET' && c.path === '/notifications/preferences') return jsonResponse(prefs())
      if (c.method === 'PUT') {
        emailOn = false
        return jsonResponse(prefs())
      }
      return undefined
    })
    renderPage('/notificacoes?aba=preferencias')
    expect(await screen.findByText('Notificações push indisponíveis')).toBeInTheDocument()
    const push = screen.getByRole('switch', { name: /Campanha aprovada — Push \(indisponível\)/ })
    expect(push).toBeDisabled()
    expect(push).toHaveAttribute('aria-checked', 'false')
    await user.click(screen.getByRole('switch', { name: 'Campanha aprovada — E-mail' }))
    await user.click(screen.getByRole('button', { name: /Salvar preferências/ }))
    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    expect(calls.find((c) => c.method === 'PUT')!.body).toEqual({ items: [{ event_type: 'campaign.approved', channel: 'EMAIL', enabled: false }] })
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Campanha aprovada — E-mail' })).toHaveAttribute('aria-checked', 'false'))
    const row = screen.getByText('Campanha aprovada').closest('tr') as HTMLElement
    expect(within(row).getAllByText('Indisponível').length).toBe(1)
  })
})
