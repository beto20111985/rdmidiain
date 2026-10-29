/**
 * Ativação do 2º Master (SPEC AÇÃO 003 §11.6): o token vem do fragmento e é removido da barra na hora,
 * nunca vai para storage/URL; o convidado cria senha, TOTP e senha financeira; erro genérico sem vazar
 * o estado do convite; códigos de recuperação uma única vez; cookie de refresh aceito (credentials: include).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router-dom'
import MasterActivationPage from './MasterActivationPage'
import { takeTokenFromFragment } from '../activationToken'
import { useAuthStore } from '@/stores/authStore'
import { useContextStore } from '@/stores/contextStore'
import { tokenStore } from '@/lib/http'
import { fetchRouter } from '@/components/finance/testing'
import { makeMasterMe } from '@/test/securityFixtures'
import { jsonResponse, renderWithProviders } from '@/test/utils'

const TOKEN = 'tok_ABC-123_secreto'
const INFO = { full_name: 'Maria Souza', email_masked: 'm***@rd.invalid', expires_at: '2026-10-11T12:00:00Z', instance_name: 'RD Mídia Indoor', steps: ['PASSWORD', 'TOTP', 'FINANCIAL_PASSWORD'] }
const TOTP = { otpauth_uri: 'otpauth://totp/RD:maria?secret=JBSWY3DPEHPK3PXP', secret: 'JBSWY3DPEHPK3PXP' }

function openActivation(hash: string) {
  window.history.replaceState(null, '', `/ativar-master${hash}`)
  return renderWithProviders(
    <Routes>
      <Route path="/ativar-master" element={<MasterActivationPage />} />
      <Route path="/login" element={<p>tela de login</p>} />
      <Route path="/" element={<p>painel</p>} />
    </Routes>,
    { route: '/ativar-master' },
  )
}

beforeEach(() => {
  tokenStore.clear()
  useContextStore.getState().reset()
  useAuthStore.setState({ status: 'anonymous', me: null, mfaToken: null, mfaExpiresAt: null, mfaSetupToken: null, setup: null, notice: null, demo: false })
})
afterEach(() => {
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

describe('takeTokenFromFragment', () => {
  it('lê #token= e remove o fragmento da barra de endereço imediatamente (preserva caminho e query)', () => {
    window.history.replaceState(null, '', '/ativar-master?x=1#token=abc%2Bdef')
    expect(takeTokenFromFragment()).toBe('abc+def')
    expect(window.location.hash).toBe('')
    expect(window.location.pathname + window.location.search).toBe('/ativar-master?x=1')
  })

  it('sem token no fragmento não mexe na URL; token na query string NÃO é aceito', () => {
    window.history.replaceState(null, '', '/ativar-master?token=naoaceito#outra=coisa')
    expect(takeTokenFromFragment()).toBeNull()
    expect(window.location.hash).toBe('#outra=coisa')
  })
})

describe('página /ativar-master', () => {
  it('remove o token da barra, não o guarda em storage e consulta o convite só com POST (token no corpo)', async () => {
    const r = fetchRouter([{ method: 'POST', match: /\/auth\/master-activation\/inspect$/, reply: () => INFO }])
    openActivation(`#token=${TOKEN}`)
    expect(window.location.hash).toBe('')
    expect(await screen.findByRole('heading', { level: 1, name: 'Ativar conta de Administrador Master' })).toBeInTheDocument()
    expect(screen.getByText(/Maria Souza/)).toBeInTheDocument()
    expect(screen.getByText(/m\*\*\*@rd\.invalid/)).toBeInTheDocument()

    const calls = r.calls
    expect(calls).toHaveLength(1)
    expect(calls[0].body).toEqual({ token: TOKEN })
    expect(calls[0].url).not.toContain(TOKEN) // nunca na URL
    expect(JSON.stringify({ ...window.localStorage, ...window.sessionStorage })).not.toContain(TOKEN)
    expect(document.body.innerHTML).not.toContain(TOKEN)
  })

  it('convite inválido/expirado: mensagem genérica, sem revelar o estado do convite', async () => {
    fetchRouter([
      { method: 'POST', match: /\/auth\/master-activation\/inspect$/, reply: () => jsonResponse({ error: { code: 'MASTER_INVITE_INVALID', message: 'Convite inválido ou expirado.' } }, 400) },
    ])
    openActivation(`#token=${TOKEN}`)
    expect(await screen.findByRole('heading', { level: 1, name: 'Convite inválido ou expirado' })).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent(/Peça ao Administrador Master que convidou você para reenviar o convite/)
    expect(document.body.textContent).not.toMatch(/revogado|aceito|bloqueado|já foi usado/i)
    expect(screen.getByRole('link', { name: 'Ir para o login' })).toBeInTheDocument()
  })

  it('sem token no fragmento: "link incompleto", sem nenhuma chamada ao servidor', async () => {
    const r = fetchRouter([])
    openActivation('')
    expect(await screen.findByRole('heading', { level: 1, name: 'Convite inválido ou expirado' })).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent(/link está incompleto/)
    expect(r.calls).toHaveLength(0)
  })

  it('fluxo completo: senha → TOTP → senha financeira → concluir; códigos de recuperação uma vez; sessão adotada', async () => {
    const user = userEvent.setup()
    const r = fetchRouter([
      { method: 'POST', match: /\/auth\/master-activation\/inspect$/, reply: () => INFO },
      { method: 'POST', match: /\/auth\/master-activation\/totp$/, reply: () => TOTP },
      {
        method: 'POST',
        match: /\/auth\/master-activation\/complete$/,
        reply: () => ({ status: 'ok', access_token: 'NEW-ACCESS', expires_in: 900, recovery_codes: ['AAAA-1111', 'BBBB-2222'] }),
      },
      { match: /\/me$/, reply: () => makeMasterMe('DUAL', 'DONE') },
    ])
    openActivation(`#token=${TOKEN}`)
    await screen.findByRole('heading', { level: 2, name: /1\. Criar senha/ })

    // 1. senha (política: 10+, letras e números)
    await user.type(screen.getByLabelText(/Sua senha de acesso/), 'curta')
    await user.type(screen.getByLabelText(/Confirme a nova senha/), 'curta')
    await user.click(screen.getByRole('button', { name: 'Continuar' }))
    expect(await screen.findByText(/Use pelo menos 10 caracteres/)).toBeInTheDocument()
    await user.clear(screen.getByLabelText(/Sua senha de acesso/))
    await user.clear(screen.getByLabelText(/Confirme a nova senha/))
    await user.type(screen.getByLabelText(/Sua senha de acesso/), 'Senha-de-acesso-2026')
    await user.type(screen.getByLabelText(/Confirme a nova senha/), 'Senha-de-acesso-2026')
    await user.click(screen.getByRole('button', { name: 'Continuar' }))

    // 2. TOTP: a chave é gerada UMA vez e o QR é local
    expect(await screen.findByRole('img', { name: /QR code/ })).toBeInTheDocument()
    expect(r.find('POST', /master-activation\/totp/)).toHaveLength(1)
    expect(r.find('POST', /master-activation\/totp/)[0].body).toEqual({ token: TOKEN })
    await user.click(screen.getByRole('button', { name: 'Já cadastrei, continuar' }))

    // 3. senha financeira diferente da de acesso
    await user.type(await screen.findByLabelText(/^Senha financeira/), 'Senha-de-acesso-2026')
    await user.type(screen.getByLabelText(/Confirme a senha financeira/), 'Senha-de-acesso-2026')
    await user.click(screen.getByRole('button', { name: 'Continuar' }))
    expect(await screen.findByText('A senha financeira deve ser diferente da senha de acesso.')).toBeInTheDocument()
    await user.clear(screen.getByLabelText(/^Senha financeira/))
    await user.clear(screen.getByLabelText(/Confirme a senha financeira/))
    await user.type(screen.getByLabelText(/^Senha financeira/), 'financeira-forte-77')
    await user.type(screen.getByLabelText(/Confirme a senha financeira/), 'financeira-forte-77')
    await user.click(screen.getByRole('button', { name: 'Continuar' }))

    // 4. concluir com o código ATUAL
    await user.type(await screen.findByLabelText(/Código atual do autenticador/), '123456')
    await user.click(screen.getByRole('button', { name: 'Ativar conta de Administrador Master' }))

    const list = await screen.findByRole('list', { name: 'Códigos de recuperação' })
    expect(within(list).getAllByRole('listitem').map((li) => li.textContent)).toEqual(['AAAA-1111', 'BBBB-2222'])
    const complete = r.find('POST', /master-activation\/complete/)
    expect(complete).toHaveLength(1)
    expect(complete[0].body).toEqual({ token: TOKEN, password: 'Senha-de-acesso-2026', totp_code: '123456', financial_password: 'financeira-forte-77' })
    // o servidor emite o cookie de refresh: a chamada precisa de credentials: include
    const init = (vi.mocked(globalThis.fetch).mock.calls.find(([u]) => String(u).endsWith('/master-activation/complete'))![1] ?? {}) as RequestInit
    expect(init.credentials).toBe('include')
    // sessão do novo Master adotada só em memória; sem código/segredo em storage
    await waitFor(() => expect(tokenStore.get()).toBe('NEW-ACCESS'))
    await waitFor(() => expect(useAuthStore.getState().status).toBe('authenticated'))
    expect(JSON.stringify({ ...window.localStorage, ...window.sessionStorage })).not.toMatch(/AAAA-1111|NEW-ACCESS|financeira-forte-77|Senha-de-acesso/)
    expect(document.body.textContent).not.toContain('financeira-forte-77')

    await user.click(screen.getByRole('checkbox', { name: /Guardei os códigos/ }))
    await user.click(screen.getByRole('button', { name: 'Ir para o painel' }))
    expect(await screen.findByText('painel')).toBeInTheDocument()
  }, 60_000)

  it('código TOTP inválido: erro junto da etapa final e o convite continua utilizável; convite bloqueado vira tela genérica', async () => {
    const user = userEvent.setup()
    let n = 0
    fetchRouter([
      { method: 'POST', match: /\/auth\/master-activation\/inspect$/, reply: () => INFO },
      { method: 'POST', match: /\/auth\/master-activation\/totp$/, reply: () => TOTP },
      {
        method: 'POST',
        match: /\/auth\/master-activation\/complete$/,
        reply: () => {
          n += 1
          return n === 1
            ? jsonResponse({ error: { code: 'INVALID_MFA_CODE', message: 'Código inválido.' } }, 400)
            : jsonResponse({ error: { code: 'MASTER_INVITE_LOCKED', message: 'Convite bloqueado.' } }, 400)
        },
      },
    ])
    openActivation(`#token=${TOKEN}`)
    await screen.findByRole('heading', { level: 2, name: /1\. Criar senha/ })
    await user.type(screen.getByLabelText(/Sua senha de acesso/), 'Senha-de-acesso-2026')
    await user.type(screen.getByLabelText(/Confirme a nova senha/), 'Senha-de-acesso-2026')
    await user.click(screen.getByRole('button', { name: 'Continuar' }))
    await user.click(await screen.findByRole('button', { name: 'Já cadastrei, continuar' }))
    await user.type(await screen.findByLabelText(/^Senha financeira/), 'financeira-forte-77')
    await user.type(screen.getByLabelText(/Confirme a senha financeira/), 'financeira-forte-77')
    await user.click(screen.getByRole('button', { name: 'Continuar' }))
    await user.type(await screen.findByLabelText(/Código atual do autenticador/), '111111')
    await user.click(screen.getByRole('button', { name: 'Ativar conta de Administrador Master' }))
    expect(await screen.findByText(/Código do autenticador inválido/)).toBeInTheDocument()
    expect(screen.getByLabelText(/Código atual do autenticador/)).toHaveValue('') // uso único
    await user.type(screen.getByLabelText(/Código atual do autenticador/), '222222')
    await user.click(screen.getByRole('button', { name: 'Ativar conta de Administrador Master' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Convite inválido ou expirado' })).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent(/bloqueado por tentativas inválidas/)
  }, 60_000)

  it('aviso quando o navegador já tem outra sessão ativa', async () => {
    fetchRouter([{ method: 'POST', match: /\/auth\/master-activation\/inspect$/, reply: () => INFO }])
    useAuthStore.setState({ status: 'authenticated', me: makeMasterMe('DUAL'), demo: false })
    openActivation(`#token=${TOKEN}`)
    expect(await screen.findByText('Este navegador já tem uma sessão ativa')).toBeInTheDocument()
    expect(screen.getByText(/Sócio A/)).toBeInTheDocument()
  })
})
