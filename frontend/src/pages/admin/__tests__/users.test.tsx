import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import UserDetailPage from '../users/UserDetailPage'
import { InviteDialog } from '../users/InviteDialog'
import { explainUserError } from '../users/userErrors'
import { ApiError } from '@/lib/errors'
import { tokenStore } from '@/lib/http'
import { useAuthStore } from '@/stores/authStore'
import { renderWithProviders } from '@/test/utils'
import { apiError, jsonResponse, mockApi, renderRoute, resetSession, signInAs } from '@/test/adminTestUtils'

const CLIENT_USER = {
  id: 'u2',
  name: 'Ana Cliente',
  email: 'ana@exemplo.invalid',
  phone: null,
  is_active: true,
  is_admin_master: false,
  mfa_enabled: false,
  must_change_password: false,
  last_login: null,
  created_at: '2026-10-01T12:00:00Z',
  locked: false,
  assignments: [{ id: 'a1', role: 'advertiser', scope_type: 'ORGANIZATION', scope_id: 'o2', scope_value: null }],
  restricted: false,
}

// Páginas completas com muitos campos: folga para execução paralela com as demais suítes.
vi.setConfig({ testTimeout: 20_000 })

beforeEach(() => resetSession())
afterEach(() => {
  vi.unstubAllGlobals()
  tokenStore.clear()
})

describe('Impersonation — motivo obrigatório', () => {
  it('só envia POST /auth/impersonation com motivo de 10+ caracteres e adota o token em memória', async () => {
    const user = userEvent.setup()
    signInAs({ permissions: ['users.view', 'users.manage', 'users.impersonate'] })
    const { calls } = mockApi((c) => {
      if (c.method === 'GET' && c.path === '/users/u2') return jsonResponse(CLIENT_USER)
      if (c.method === 'POST' && c.path === '/auth/impersonation')
        return jsonResponse({
          access_token: 'TOKEN-IMP',
          token_type: 'bearer',
          expires_in: 900,
          impersonation: { active: true, session_id: 's9', real_user: { id: 'u1', name: 'Pessoa Teste' }, user: { id: 'u2' }, reason: 'x' },
        })
      if (c.method === 'GET' && c.path === '/me')
        return jsonResponse({
          user: { id: 'u2', name: 'Ana Cliente', email: 'ana@exemplo.invalid', mfa_enabled: false, is_admin_master: false },
          permissions: ['campaigns.view'],
          assignments: [],
          contexts: [{ key: 'adv:o2', label: 'Minhas campanhas — Loja', kind: 'ADVERTISER', org_id: 'o2' }],
          impersonation: { active: true, real_user: { id: 'u1', name: 'Pessoa Teste' }, reason: 'x' },
          mfa: { required: false, configured: false },
        })
      return undefined
    })
    renderRoute(<UserDetailPage />, '/usuarios/:id', '/usuarios/u2')

    await user.click(await screen.findByRole('button', { name: /Acessar como este usuário/ }))
    const dialog = await screen.findByRole('dialog')
    const confirm = within(dialog).getByRole('button', { name: 'Iniciar acesso' })
    expect(confirm).toBeDisabled()
    expect(within(dialog).getByText(/registrados na auditoria/)).toBeInTheDocument()

    const reason = within(dialog).getByLabelText(/Motivo do acesso/)
    await user.type(reason, 'curto')
    await user.tab()
    expect(confirm).toBeDisabled()
    expect(within(dialog).getByText(/pelo menos 10 caracteres/)).toBeInTheDocument()
    expect(calls.some((c) => c.path === '/auth/impersonation')).toBe(false)

    await user.clear(reason)
    await user.type(reason, 'Chamado CH-ABC123: cliente não vê a campanha')
    expect(confirm).toBeEnabled()
    await user.click(confirm)

    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.path === '/auth/impersonation')).toBe(true))
    const post = calls.find((c) => c.path === '/auth/impersonation')!
    expect(post.body).toEqual({ user_id: 'u2', reason: 'Chamado CH-ABC123: cliente não vê a campanha' })
    await waitFor(() => expect(useAuthStore.getState().me?.impersonation.active).toBe(true))
    expect(tokenStore.get()).toBe('TOKEN-IMP')
    expect(window.localStorage.length).toBe(0)
  })

  it('administradores e Admin Master não podem ser personificados (botão desabilitado com o motivo)', async () => {
    signInAs({ permissions: ['users.view', 'users.impersonate'] })
    mockApi((c) =>
      c.path === '/users/u3'
        ? jsonResponse({ ...CLIENT_USER, id: 'u3', name: 'Beto Admin', assignments: [{ id: 'a9', role: 'admin', scope_type: 'GLOBAL', scope_id: null, scope_value: null }] })
        : undefined,
    )
    renderRoute(<UserDetailPage />, '/usuarios/:id', '/usuarios/u3')
    const btn = await screen.findByRole('button', { name: /Acessar como este usuário/ })
    expect(btn).toBeDisabled()
    expect(screen.getByText(/Não é permitido acessar como administradores/)).toBeInTheDocument()
  })

  it('Admin Master visto pelo outro: só nome e situação, sem ações, com a explicação de independência', async () => {
    signInAs({ permissions: ['users.view', 'users.manage', 'users.impersonate'], user: { is_admin_master: true } })
    mockApi((c) => (c.path === '/users/m2' ? jsonResponse({ id: 'm2', name: 'Sócio B', is_active: true, is_admin_master: true, restricted: true }) : undefined))
    renderRoute(<UserDetailPage />, '/usuarios/:id', '/usuarios/m2')
    expect(await screen.findByText('Os dois Admin Master são independentes')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Acessar como/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Desativar/ })).not.toBeInTheDocument()
  })
})

describe('Convite — erro de escalonamento', () => {
  it('mostra o motivo do 403 ROLE_ESCALATION devolvido pelo servidor', async () => {
    const user = userEvent.setup()
    signInAs({ permissions: ['users.view', 'users.manage'], assignments: [{ role: 'admin', scope_type: 'GLOBAL' }] })
    const { calls } = mockApi((c) => {
      if (c.path === '/roles')
        return jsonResponse({
          items: [
            {
              id: 'r1',
              slug: 'admin',
              name: 'Administrador',
              description: 'Gestão da plataforma.',
              is_system: true,
              mfa_required: true,
              permissions: ['users.manage', 'finance.approve'],
              typical_scopes: ['GLOBAL'],
            },
          ],
        })
      if (c.method === 'POST' && c.path === '/auth/invites') return apiError(403, 'ROLE_ESCALATION', 'Você não pode conceder este perfil.')
      return undefined
    })
    renderWithProviders(<InviteDialog open onOpenChange={() => undefined} />)
    await user.type(screen.getByLabelText(/E-mail/), 'novo@exemplo.invalid')
    await user.click(screen.getByRole('combobox', { name: /Perfil/ }))
    await user.click(await screen.findByRole('option', { name: /Administrador/ }))
    await user.click(screen.getByRole('button', { name: 'Enviar convite' }))

    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText('Perfil acima do seu acesso')).toBeInTheDocument()
    expect(within(alert).getByText(/Você não pode conceder este perfil\./)).toBeInTheDocument()
    expect(within(alert).getByText(/Código: ROLE_ESCALATION/)).toBeInTheDocument()
    const post = calls.find((c) => c.method === 'POST')!
    expect(post.body).toMatchObject({ email: 'novo@exemplo.invalid', role: 'admin', scope_type: 'GLOBAL' })
  })

  it('Super Admin não é oferecido (reservado aos Admin Master) e o 403 ROLE_RESERVED_FOR_ADMIN_MASTER é explicado', async () => {
    const user = userEvent.setup()
    signInAs({ permissions: ['users.view', 'users.manage'], assignments: [{ role: 'admin', scope_type: 'GLOBAL' }] })
    const { calls } = mockApi((c) => {
      if (c.path === '/roles')
        return jsonResponse({
          items: [
            { id: 'r1', slug: 'super_admin', name: 'Super Admin', description: 'Acesso total.', is_system: true, mfa_required: true, permissions: [], typical_scopes: ['GLOBAL'] },
            { id: 'r2', slug: 'designer', name: 'Designer', description: 'Artes.', is_system: true, mfa_required: false, permissions: [], typical_scopes: ['GLOBAL'] },
          ],
        })
      if (c.method === 'POST' && c.path === '/auth/invites')
        return apiError(403, 'ROLE_RESERVED_FOR_ADMIN_MASTER', 'O perfil Super Admin é exclusivo dos Administradores Master.')
      return undefined
    })
    renderWithProviders(<InviteDialog open onOpenChange={() => undefined} />)
    await user.click(screen.getByRole('combobox', { name: /Perfil/ }))
    expect(await screen.findByRole('option', { name: /Designer/ })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /Super Admin/ })).not.toBeInTheDocument()
    expect(calls.some((c) => c.method === 'POST')).toBe(false)
  })

  it('o erro ROLE_RESERVED_FOR_ADMIN_MASTER (ex.: cliente desatualizado) vira uma explicação clara', () => {
    const e = explainUserError(new ApiError({ status: 403, code: 'ROLE_RESERVED_FOR_ADMIN_MASTER', message: '' }))
    expect(e.title).toBe('Perfil reservado')
    expect(e.detail).toMatch(/exclusivo dos Administradores Master/)
  })

  it('valida o e-mail e o escopo antes de enviar (sem chamada à API)', async () => {
    const user = userEvent.setup()
    signInAs({ permissions: ['users.view', 'users.manage'] })
    const { calls } = mockApi((c) => (c.path === '/roles' ? jsonResponse({ items: [] }) : undefined))
    renderWithProviders(<InviteDialog open onOpenChange={() => undefined} />)
    await user.click(screen.getByRole('button', { name: 'Enviar convite' }))
    expect(await screen.findByText('Informe um e-mail válido.')).toBeInTheDocument()
    expect(screen.getByText('Escolha o perfil.')).toBeInTheDocument()
    expect(calls.some((c) => c.path === '/auth/invites')).toBe(false)
  })
})
