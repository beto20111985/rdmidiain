/**
 * Assistente de primeiro acesso (SPEC AÇÃO 003 §11.2): passos vindos do SERVIDOR, na ordem imposta,
 * com progresso; passo 4 só na DUPLA para Master; SINGLE sem nenhuma menção a segundo Master;
 * token de setup renovado a cada troca de senha; códigos de recuperação exibidos uma vez e fora do storage.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router-dom'
import FirstAccessWizardPage from './FirstAccessWizardPage'
import { useAuthStore } from '@/stores/authStore'
import { useContextStore } from '@/stores/contextStore'
import { tokenStore } from '@/lib/http'
import { fetchRouter } from '@/components/finance/testing'
import { useSecurityUiStore } from '@/features/security/uiStore'
import { freshMasterSetup, makeMasterMe, makeSetup, masterSetup } from '@/test/securityFixtures'
import { makeMe, renderWithProviders } from '@/test/utils'
import { jsonResponse } from '@/test/utils'

function renderWizard() {
  return renderWithProviders(
    <Routes>
      <Route path="/primeiro-acesso" element={<FirstAccessWizardPage />} />
      <Route path="/login" element={<p>tela de login</p>} />
      <Route path="/" element={<p>painel</p>} />
    </Routes>,
    { route: '/primeiro-acesso' },
  )
}

const STRONG = 'Senha-nova-segura-2026'

beforeEach(() => {
  tokenStore.clear()
  useContextStore.getState().reset()
  useSecurityUiStore.getState().reset()
  useAuthStore.setState({ status: 'anonymous', me: null, mfaToken: null, mfaExpiresAt: null, mfaSetupToken: null, setup: null, notice: null, demo: false })
})
afterEach(() => vi.unstubAllGlobals())

describe('assistente de primeiro acesso', () => {
  it('Master na DUPLA: mostra os 4 passos com progresso; só o passo atual aparece; senha → novo token de setup → TOTP', async () => {
    const user = userEvent.setup()
    useAuthStore.setState({ status: 'setup_required', mfaSetupToken: 'SETUP-1', setup: freshMasterSetup('DUAL') })
    const next = makeSetup({ ...freshMasterSetup('DUAL'), next_step: 'TOTP', steps: { password_change: 'DONE', totp: 'PENDING', financial_password: 'PENDING', second_master: 'NOT_INVITED' } })
    const r = fetchRouter([
      { method: 'POST', match: /\/auth\/setup\/password$/, reply: () => ({ status: 'mfa_setup_required', mfa_setup_token: 'SETUP-2', expires_in: 600, setup: next }) },
    ])
    renderWizard()

    expect(screen.getByRole('heading', { level: 1, name: 'Primeiro acesso' })).toBeInTheDocument()
    const steps = screen.getByRole('list', { name: 'Etapas' })
    expect(within(steps).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      expect.stringContaining('Alterar senha'),
      expect.stringContaining('Configurar TOTP'),
      expect.stringContaining('Criar senha financeira'),
      expect.stringContaining('Convidar segundo Master'),
    ])
    expect(within(steps).getAllByRole('listitem')[0]).toHaveAttribute('aria-current', 'step')
    expect(screen.getByRole('heading', { level: 2, name: /1\. Alterar senha/ })).toBeInTheDocument()
    // só o formulário do passo atual
    expect(screen.queryByLabelText(/Código do aplicativo/)).not.toBeInTheDocument()

    await user.type(screen.getByLabelText(/Senha atual \(provisória\)/), 'Provisoria-123')
    await user.type(screen.getByLabelText(/^Nova senha/), STRONG)
    await user.type(screen.getByLabelText(/Confirme a nova senha/), STRONG)
    await user.click(screen.getByRole('button', { name: 'Salvar nova senha' }))

    const call = r.find('POST', /\/auth\/setup\/password$/)[0]
    expect(call.body).toEqual({ current_password: 'Provisoria-123', new_password: STRONG })
    expect(call.headers.Authorization).toBe('Bearer SETUP-1')
    expect(call.headers['X-RD-Context']).toBeUndefined()
    // novo token (o antigo não vale mais) e próximo passo vindo do servidor
    await waitFor(() => expect(useAuthStore.getState().mfaSetupToken).toBe('SETUP-2'))
    expect(await screen.findByRole('heading', { level: 2, name: /2\. Configurar o autenticador/ })).toBeInTheDocument()
    expect(screen.queryByLabelText(/Nova senha/)).not.toBeInTheDocument()
    expect(within(screen.getByRole('list', { name: 'Etapas' })).getAllByRole('listitem')[0]).toHaveTextContent('concluída')
    // nada em armazenamento do navegador
    expect(window.localStorage.length).toBe(0)
    expect(window.sessionStorage.length).toBe(0)
  }, 30_000)

  it('TOTP: QR local, códigos de recuperação UMA vez (fora do storage) e adota a sessão devolvida → senha financeira', async () => {
    const user = userEvent.setup()
    const afterTotp = makeMasterMe('DUAL', 'NOT_INVITED', {
      security_setup: makeSetup({ ...freshMasterSetup('DUAL'), next_step: 'FINANCIAL_PASSWORD', steps: { password_change: 'DONE', totp: 'DONE', financial_password: 'PENDING', second_master: 'NOT_INVITED' } }),
    })
    useAuthStore.setState({
      status: 'setup_required',
      mfaSetupToken: 'SETUP-2',
      setup: makeSetup({ ...freshMasterSetup('DUAL'), next_step: 'TOTP', steps: { password_change: 'DONE', totp: 'PENDING', financial_password: 'PENDING', second_master: 'NOT_INVITED' } }),
    })
    const r = fetchRouter([
      { method: 'POST', match: /\/auth\/mfa\/totp\/setup$/, reply: () => ({ otpauth_uri: 'otpauth://totp/RD:master?secret=JBSWY3DPEHPK3PXP', secret: 'JBSWY3DPEHPK3PXP' }) },
      { method: 'POST', match: /\/auth\/mfa\/totp\/enable$/, reply: () => ({ status: 'ok', access_token: 'FULL', expires_in: 900, recovery_codes: ['AAAA-1111', 'BBBB-2222'] }) },
      { match: /\/me$/, reply: () => afterTotp },
    ])
    renderWizard()
    await user.type(screen.getByLabelText(/Confirme sua senha/), STRONG)
    await user.click(screen.getByRole('button', { name: 'Continuar' }))
    expect(await screen.findByRole('img', { name: /QR code/ })).toBeInTheDocument()
    expect(r.find('POST', /totp\/setup/)[0].headers.Authorization).toBe('Bearer SETUP-2')
    expect(r.find('POST', /totp\/setup/)[0].body).toEqual({ password: STRONG })

    await user.type(screen.getByLabelText(/Código do aplicativo/), '123456')
    await user.click(screen.getByRole('button', { name: 'Ativar verificação' }))
    const list = await screen.findByRole('list', { name: 'Códigos de recuperação' })
    expect(within(list).getAllByRole('listitem').map((li) => li.textContent)).toEqual(['AAAA-1111', 'BBBB-2222'])
    expect(r.find('POST', /totp\/enable/)[0].body).toEqual({ code: '123456' })
    // códigos NUNCA em armazenamento do navegador
    expect(JSON.stringify({ ...window.localStorage })).not.toContain('AAAA-1111')
    expect(JSON.stringify({ ...window.sessionStorage })).not.toContain('AAAA-1111')
    // sem confirmar que guardou, não segue
    expect(screen.getByRole('button', { name: 'Continuar' })).toBeDisabled()
    await user.click(screen.getByRole('checkbox', { name: /Guardei os códigos/ }))
    await user.click(screen.getByRole('button', { name: 'Continuar' }))

    expect(await screen.findByRole('heading', { level: 2, name: /Criar a senha financeira/ })).toBeInTheDocument()
    expect(tokenStore.get()).toBe('FULL')
    expect(useAuthStore.getState().mfaSetupToken).toBeNull()
    expect(screen.queryByRole('list', { name: 'Códigos de recuperação' })).not.toBeInTheDocument()
  }, 30_000)

  it('senha financeira → /me sem pendência bloqueante → passo 4 (DUPLA): convite ao 2º Master ou "Fazer depois"', async () => {
    const user = userEvent.setup()
    tokenStore.set('FULL')
    useAuthStore.setState({
      status: 'setup_required',
      me: makeMasterMe('DUAL', 'NOT_INVITED', { security_setup: makeSetup({ ...freshMasterSetup('DUAL'), next_step: 'FINANCIAL_PASSWORD', steps: { password_change: 'DONE', totp: 'DONE', financial_password: 'PENDING', second_master: 'NOT_INVITED' } }) }),
      setup: makeSetup({ ...freshMasterSetup('DUAL'), next_step: 'FINANCIAL_PASSWORD', steps: { password_change: 'DONE', totp: 'DONE', financial_password: 'PENDING', second_master: 'NOT_INVITED' } }),
    })
    const done = masterSetup('DUAL', 'NOT_INVITED')
    const r = fetchRouter([
      { method: 'POST', match: /\/auth\/setup\/financial-password$/, reply: () => ({ status: 'ok', setup: done }) },
      { match: /\/me$/, reply: () => makeMasterMe('DUAL', 'NOT_INVITED') },
    ])
    renderWizard()
    expect(screen.getByRole('heading', { level: 2, name: /Criar a senha financeira/ })).toBeInTheDocument()
    await user.type(screen.getByLabelText(/Senha de login/), STRONG)
    await user.type(screen.getByLabelText(/Código do autenticador/), '654321')
    await user.type(screen.getByLabelText(/^Senha financeira/), 'financeira-forte-1')
    await user.type(screen.getByLabelText(/Confirme a senha financeira/), 'financeira-forte-1')
    await user.click(screen.getByRole('button', { name: 'Criar senha financeira' }))

    expect(r.find('POST', /financial-password/)[0].body).toEqual({ login_password: STRONG, totp_code: '654321', financial_password: 'financeira-forte-1' })
    // passo 4 (não bloqueante) aparece na DUPLA, para o Master
    expect(await screen.findByRole('heading', { level: 2, name: /Convidar o segundo Administrador Master/ })).toBeInTheDocument()
    expect(screen.getByLabelText(/Nome completo do convidado/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Fazer depois' }))
    expect(await screen.findByText('painel')).toBeInTheDocument()
    // "Fazer depois" também silencia o modal nesta sessão
    expect(useSecurityUiStore.getState().secondMasterModalDismissed).toBe(true)
  }, 30_000)

  it('SINGLE: 3 passos, nenhuma menção a segundo Master em NENHUM passo; sem passo 4 mesmo se o servidor mandar', async () => {
    const user = userEvent.setup()
    useAuthStore.setState({ status: 'setup_required', mfaSetupToken: 'SETUP-1', setup: freshMasterSetup('SINGLE') })
    renderWizard()
    const steps = within(screen.getByRole('list', { name: 'Etapas' })).getAllByRole('listitem')
    expect(steps).toHaveLength(3)
    expect(document.body.textContent).not.toMatch(/segundo|2º Master|Convidar/i)
    await user.type(screen.getByLabelText(/Senha atual/), 'x')
    expect(document.body.textContent).not.toMatch(/segundo|2º Master|Convidar/i)

    // sessão autenticada na ÚNICA com next_step=SECOND_MASTER inconsistente: não mostra o passo, vai ao painel
    tokenStore.set('A1')
    useAuthStore.setState({ status: 'authenticated', mfaSetupToken: null, setup: { ...masterSetup('SINGLE', 'NOT_REQUIRED'), next_step: 'SECOND_MASTER' }, me: makeMasterMe('SINGLE') })
    expect(await screen.findByText('painel')).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/segundo|2º Master/i)
  }, 30_000)

  it('usuário comum só com TOTP pendente vê apenas esse passo (sem lista de progresso)', async () => {
    useAuthStore.setState({
      status: 'setup_required',
      mfaSetupToken: 'SETUP-1',
      setup: makeSetup({ required: true, next_step: 'TOTP', steps: { password_change: 'DONE', totp: 'PENDING' } }),
    })
    renderWizard()
    expect(screen.getByRole('heading', { level: 2, name: /Configurar o autenticador/ })).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Etapas' })).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { level: 2, name: /Alterar senha/ })).not.toBeInTheDocument()
  })

  it('erros do servidor ficam junto do campo; token de setup expirado volta ao login', async () => {
    const user = userEvent.setup()
    useAuthStore.setState({ status: 'setup_required', mfaSetupToken: 'SETUP-1', setup: freshMasterSetup('SINGLE') })
    let attempt = 0
    fetchRouter([
      {
        method: 'POST',
        match: /\/auth\/setup\/password$/,
        reply: () => {
          attempt += 1
          if (attempt === 1) return jsonResponse({ error: { code: 'INVALID_PASSWORD', message: 'Senha atual incorreta.' } }, 400)
          if (attempt === 2) return jsonResponse({ error: { code: 'PASSWORD_REUSED', message: 'A nova senha deve ser diferente da atual.' } }, 400)
          return jsonResponse({ error: { code: 'INVALID_TOKEN', message: 'Token inválido.' } }, 401)
        },
      },
    ])
    renderWizard()
    const fill = async () => {
      await user.clear(screen.getByLabelText(/Senha atual/))
      await user.type(screen.getByLabelText(/Senha atual/), 'Provisoria-123')
      await user.clear(screen.getByLabelText(/^Nova senha/))
      await user.type(screen.getByLabelText(/^Nova senha/), STRONG)
      await user.clear(screen.getByLabelText(/Confirme a nova senha/))
      await user.type(screen.getByLabelText(/Confirme a nova senha/), STRONG)
      await user.click(screen.getByRole('button', { name: 'Salvar nova senha' }))
    }
    await fill()
    expect(await screen.findByText('Senha atual incorreta.')).toBeInTheDocument()
    expect(screen.getByLabelText(/Senha atual/)).toHaveAttribute('aria-invalid', 'true')
    await fill()
    expect(await screen.findByText('A nova senha deve ser diferente da atual.')).toBeInTheDocument()
    expect(screen.getByLabelText(/^Nova senha/)).toHaveAttribute('aria-invalid', 'true')
    await fill()
    expect(await screen.findByText('tela de login')).toBeInTheDocument()
    expect(useAuthStore.getState().notice).toBe('expired')
    expect(useAuthStore.getState().mfaSetupToken).toBeNull()
  }, 40_000)

  it('sem passo pendente (e sem 2º Master a configurar) vai ao painel; anônimo vai ao login', () => {
    useAuthStore.setState({ status: 'authenticated', me: makeMe({ security_setup: makeSetup() }), setup: makeSetup() })
    renderWizard()
    expect(screen.getByText('painel')).toBeInTheDocument()
  })

  it('anônimo é levado ao login', () => {
    renderWizard()
    expect(screen.getByText('tela de login')).toBeInTheDocument()
  })
})
