import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router-dom'
import LoginPage from './LoginPage'
import { useAuthStore } from '@/stores/authStore'
import { tokenStore } from '@/lib/http'
import { jsonResponse, mockFetch, renderWithProviders } from '@/test/utils'
import { loginErrorMessage } from '../authErrors'
import { ApiError } from '@/lib/errors'

beforeEach(() => {
  tokenStore.clear()
  useAuthStore.setState({ status: 'anonymous', me: null, notice: null, demo: false, mfaToken: null })
})
afterEach(() => vi.unstubAllGlobals())

function renderLogin() {
  return renderWithProviders(
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/login/verificacao" element={<p>tela de verificação</p>} />
      <Route path="/telas" element={<p>tela de telas</p>} />
    </Routes>,
    { route: '/login?next=%2Ftelas' },
  )
}

describe('LoginPage', () => {
  it('401 do login mostra a mensagem na própria tela (sem redirecionar nem limpar)', async () => {
    const user = userEvent.setup()
    mockFetch(() => jsonResponse({ error: { code: 'AUTH_INVALID_CREDENTIALS', message: 'E-mail, telefone ou senha incorretos.' } }, 401))
    renderLogin()
    await user.type(screen.getByLabelText(/E-mail ou telefone/), 'pessoa@exemplo.invalid')
    await user.type(screen.getByLabelText(/^Senha/), 'errada')
    await user.click(screen.getByRole('button', { name: 'Entrar' }))
    expect(await screen.findByText('E-mail, telefone ou senha incorretos.')).toBeInTheDocument()
    expect(screen.getByLabelText(/E-mail ou telefone/)).toHaveValue('pessoa@exemplo.invalid')
    expect(screen.getByRole('heading', { level: 1, name: 'Entrar no painel' })).toBeInTheDocument()
  })

  it('valida campos obrigatórios com erro junto ao campo', async () => {
    const user = userEvent.setup()
    renderLogin()
    await user.click(screen.getByRole('button', { name: 'Entrar' }))
    expect(await screen.findByText('Informe seu e-mail ou telefone.')).toBeInTheDocument()
    expect(screen.getByLabelText(/E-mail ou telefone/)).toHaveAttribute('aria-invalid', 'true')
  })

  it('mfa_required leva à verificação preservando o destino', async () => {
    const user = userEvent.setup()
    mockFetch(() => jsonResponse({ status: 'mfa_required', mfa_token: 'M', expires_in: 300 }))
    renderLogin()
    await user.type(screen.getByLabelText(/E-mail ou telefone/), 'adm@rd.invalid')
    await user.type(screen.getByLabelText(/^Senha/), 'x')
    await user.click(screen.getByRole('button', { name: 'Entrar' }))
    expect(await screen.findByText('tela de verificação')).toBeInTheDocument()
  })
})

describe('mensagens de bloqueio', () => {
  it('423 informa o tempo de bloqueio; 429 informa a espera', () => {
    const now = new Date('2026-10-07T15:00:00Z')
    const locked = new ApiError({ status: 423, code: 'ACCOUNT_LOCKED', message: 'x', details: { retry_after_s: 900 } })
    expect(loginErrorMessage(locked, now)).toMatch(/Tente novamente em 15 minutos \(às 12:15\)/)
    const limited = new ApiError({ status: 429, code: 'RATE', message: 'x', details: { retry_after_s: 30 } })
    expect(loginErrorMessage(limited)).toMatch(/Aguarde 30 segundos/)
  })
})
