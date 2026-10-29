import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent, { type UserEvent } from '@testing-library/user-event'
import { Route, Routes, useLocation } from 'react-router-dom'
import { headersOf, jsonResponse, makeMe, renderWithProviders } from '@/test/utils'
import { tokenStore } from '@/lib/http'
import { useAuthStore } from '@/stores/authStore'
import { apiError, callsTo, routeFetch } from '@/test/opsTestUtils'
import AdvertiserSignupPage from '../advertiser-signup/AdvertiserSignupPage'
import { signupPasswordProblems, stepForError } from '../advertiser-signup/rules'
import LoginPage from '@/features/auth/pages/LoginPage'

// Fluxos de várias etapas digitados tecla a tecla: limite explícito por teste (determinístico; o padrão de
// 5 s estoura quando outros agentes usam a CPU do container ao mesmo tempo).
vi.setConfig({ testTimeout: 30_000 })

const TOKEN = 'tok-0123456789-abcdefghij-xyz'
const POLICY = { min_length: 10, max_length: 128, requires_letters_and_digits: true, must_not_contain_email: true }
const TERMS = {
  advertiser_terms: { id: 'tpl-1', kind: 'ADVERTISER_TERMS', version: 2, title: 'Termos do Anunciante', body: 'Cláusula 1. Texto <i>literal</i>.', body_sha256: 'x' },
  terms_version: '2026-10-01',
  privacy_version: '2026-10-01',
  password_policy: POLICY,
  captcha_required: false,
  token_header: 'X-RD-Signup-Token',
}
const OPTIONS = {
  segments: [
    { id: 'seg-1', parent_id: null, name: 'Alimentos', depth: 0 },
    { id: 'seg-2', parent_id: null, name: 'Bebidas', depth: 0 },
  ],
  categories: [
    { id: 'sec-1', parent_id: null, name: 'Alimentação', depth: 0, kind: 'SECTOR' },
    { id: 'cat-1', parent_id: 'sec-1', name: 'Restaurantes', depth: 1, kind: 'CATEGORY' },
  ],
}
const COMPLETE_OK = {
  status: 'ok',
  access_token: 'acc-novo',
  token_type: 'bearer',
  expires_in: 900,
  user: { id: 'u9' },
  organization: { id: 'org-9', name: 'Mercado Bom Preço', business_roles: ['ADVERTISER'], document_type: 'CNPJ', kyc_status: 'PENDING' },
  terms_acceptance: { id: 'acc-1', version: 2, pdf_sha256: 'y' },
  sales_contact_requested: false,
  next_steps: [],
}

function Probe() {
  const loc = useLocation()
  return <p data-testid="url">{loc.pathname + loc.search}</p>
}

function renderSignup(route: string) {
  return renderWithProviders(
    <Routes>
      <Route
        path="/cadastro-anunciante"
        element={
          <>
            <AdvertiserSignupPage />
            <Probe />
          </>
        }
      />
      <Route path="/anunciante/campanhas/nova" element={<p>nova campanha</p>} />
    </Routes>,
    { route },
  )
}


/** Preenche todas as etapas com dados válidos (fireEvent nos campos de texto = rápido e determinístico). */
async function fillValid(user: UserEvent) {
  const type = (re: RegExp, value: string) => fireEvent.change(screen.getByLabelText(re), { target: { value } })
  await screen.findByRole('heading', { level: 2, name: 'Empresa e contato' })
  type(/^CNPJ/, '12.ABC.345/01DE-35')
  type(/Razão social/, 'Mercado Bom Preço')
  type(/^Telefone/, '11999998888')
  await user.click(screen.getByRole('combobox'))
  await user.click(await screen.findByRole('option', { name: 'SP — São Paulo' }))
  type(/^Cidade/, 'Campinas')
  await user.click(screen.getByRole('button', { name: 'Continuar' }))
  await user.click(await screen.findByRole('checkbox', { name: 'Alimentos' }))
  await user.click(screen.getByRole('button', { name: 'Continuar' }))
  await screen.findByRole('heading', { level: 2, name: 'Seu acesso' })
  type(/Seu nome/, 'Ana Lima')
  type(/^Senha/, 'Segura2026xyz')
  type(/Confirme a senha/, 'Segura2026xyz')
  await user.click(screen.getByRole('button', { name: 'Continuar' }))
  await user.click(await screen.findByRole('checkbox', { name: /Termos do Anunciante/ }))
  await user.click(screen.getByRole('checkbox', { name: /Termos de uso/ }))
  await user.click(screen.getByRole('button', { name: 'Continuar' }))
}

beforeEach(() => {
  tokenStore.clear()
  useAuthStore.setState({ status: 'anonymous', me: null, demo: false })
})
afterEach(() => vi.unstubAllGlobals())

describe('regras do auto-cadastro do anunciante', () => {
  it('senha segue a política devolvida pelo servidor', () => {
    expect(signupPasswordProblems('curta1', 'ana@ex.invalid', POLICY)[0]).toMatch(/mínimo 10/)
    expect(signupPasswordProblems('somenteletras', 'ana@ex.invalid', POLICY)).toContain('A senha deve conter letras e números.')
    expect(signupPasswordProblems('ana12345678x', 'ana@ex.invalid', POLICY)).toContain('A senha não pode conter o e-mail.')
    expect(signupPasswordProblems('Segura2026xyz', 'ana@ex.invalid', POLICY)).toEqual([])
  })
  it('erros do servidor levam à etapa certa', () => {
    expect(stepForError('DOCUMENT_ALREADY_REGISTERED')).toBe('empresa')
    expect(stepForError('PASSWORD_POLICY')).toBe('acesso')
    expect(stepForError('TERMS_VERSION_OUTDATED')).toBe('termos')
    expect(stepForError('VALIDATION_ERROR', 'organization.segment_ids')).toBe('segmentos')
  })
})

describe('/cadastro-anunciante', () => {
  it('login oferece "Criar conta de anunciante"', () => {
    renderWithProviders(
      <Routes>
        <Route path="/login" element={<LoginPage />} />
      </Routes>,
      { route: '/login' },
    )
    expect(screen.getByRole('link', { name: 'Criar conta de anunciante' })).toHaveAttribute('href', '/cadastro-anunciante')
  })

  it('sem token: pede o e-mail e responde de forma neutra (não revela se a conta existe)', async () => {
    const user = userEvent.setup()
    const f = routeFetch([
      { path: '/advertiser-signup/terms', reply: () => ({ ...TERMS, advertiser_terms: null }) },
      { method: 'POST', path: '/advertiser-signup/start', reply: () => jsonResponse({ status: 'accepted', message: 'Se o e-mail for válido, você receberá as instruções em instantes.' }, 202) },
    ])
    renderSignup('/cadastro-anunciante')
    await user.type(await screen.findByLabelText(/^E-mail/), 'Ana@Exemplo.invalid')
    await user.click(screen.getByRole('button', { name: 'Enviar link de cadastro' }))
    expect(await screen.findByText('Confira seu e-mail')).toBeInTheDocument()
    expect(screen.getByText(/Se o e-mail for válido/)).toBeInTheDocument()
    const [, init] = callsTo(f, '/advertiser-signup/start', 'POST')[0]
    expect(JSON.parse(String(init!.body))).toEqual({ email: 'ana@exemplo.invalid' })
    expect(headersOf(init as RequestInit).Authorization).toBeUndefined()
  })

  it('com token: sai da URL, vai só no header, conclui o cadastro, abre a sessão e oferece self-service ou comercial', async () => {
    const user = userEvent.setup()
    const f = routeFetch([
      { path: '/advertiser-signup/terms', reply: () => TERMS },
      { path: '/advertiser-signup/options', reply: () => OPTIONS },
      { path: '/advertiser-signup/application', reply: () => ({ email: 'ana@exemplo.invalid', expires_at: '2026-10-08T12:00:00Z' }) },
      { method: 'POST', path: '/advertiser-signup/complete', reply: () => jsonResponse(COMPLETE_OK, 201) },
      { path: '/me', reply: () => makeMe({ permissions: ['campaigns.view'], contexts: [{ key: 'adv:org-9', label: 'Mercado', kind: 'ADVERTISER', org_id: 'org-9' }] }) },
      { method: 'POST', path: '/advertiser-signup/sales-contact', reply: () => ({ status: 'requested', already_requested: false, message: 'O comercial vai entrar em contato.' }) },
    ])
    renderSignup(`/cadastro-anunciante?token=${TOKEN}`)
    expect(await screen.findByRole('heading', { level: 2, name: 'Empresa e contato' })).toBeInTheDocument()
    expect(screen.getByTestId('url')).toHaveTextContent(/^\/cadastro-anunciante$/)
    const [appUrl, appInit] = callsTo(f, '/advertiser-signup/application')[0]
    expect(String(appUrl)).not.toContain(TOKEN)
    expect(headersOf(appInit as RequestInit)['X-RD-Signup-Token']).toBe(TOKEN)

    // validação no cliente
    await user.click(screen.getByRole('button', { name: 'Continuar' }))
    expect(await screen.findByText('Informe a razão social.')).toBeInTheDocument()

    await user.type(screen.getByLabelText(/^CNPJ/), '12.ABC.345/01DE-35')
    await user.type(screen.getByLabelText(/Razão social/), 'Mercado Bom Preço')
    await user.type(screen.getByLabelText(/^Telefone/), '11999998888')
    await user.click(screen.getByRole('combobox'))
    await user.click(await screen.findByRole('option', { name: 'SP — São Paulo' }))
    await user.type(screen.getByLabelText(/^Cidade/), 'Campinas')
    await user.click(screen.getByRole('button', { name: 'Continuar' }))

    expect(await screen.findByRole('heading', { level: 2, name: 'Segmentos' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Continuar' }))
    expect(await screen.findByText(/pelo menos um segmento/)).toBeInTheDocument()
    await user.click(screen.getByRole('checkbox', { name: 'Alimentos' }))
    await user.click(screen.getByRole('button', { name: 'Continuar' }))

    expect(await screen.findByRole('heading', { level: 2, name: 'Seu acesso' })).toBeInTheDocument()
    await user.type(screen.getByLabelText(/Seu nome/), 'Ana Lima')
    await user.type(screen.getByLabelText(/^Senha/), 'ana12345678x')
    await user.type(screen.getByLabelText(/Confirme a senha/), 'ana12345678x')
    await user.click(screen.getByRole('button', { name: 'Continuar' }))
    expect(await screen.findByText(/não pode conter o e-mail/)).toBeInTheDocument()
    await user.clear(screen.getByLabelText(/^Senha/))
    await user.type(screen.getByLabelText(/^Senha/), 'Segura2026xyz')
    await user.clear(screen.getByLabelText(/Confirme a senha/))
    await user.type(screen.getByLabelText(/Confirme a senha/), 'Segura2026xyz')
    await user.click(screen.getByRole('button', { name: 'Continuar' }))

    expect(await screen.findByRole('heading', { level: 2, name: 'Termos e privacidade' })).toBeInTheDocument()
    expect(screen.getByText('Cláusula 1. Texto <i>literal</i>.')).toBeInTheDocument() // texto, nunca HTML
    await user.click(screen.getByRole('checkbox', { name: /Termos do Anunciante \(versão 2\)/ }))
    await user.click(screen.getByRole('checkbox', { name: /Termos de uso \(versão 2026-10-01\)/ }))
    await user.click(screen.getByRole('button', { name: 'Continuar' }))

    await user.click(await screen.findByRole('button', { name: 'Criar conta de anunciante' }))
    await waitFor(() => expect(callsTo(f, '/advertiser-signup/complete', 'POST')).toHaveLength(1))
    const [, init] = callsTo(f, '/advertiser-signup/complete', 'POST')[0]
    expect((init as RequestInit).credentials).toBe('include')
    expect(headersOf(init as RequestInit)['X-RD-Signup-Token']).toBe(TOKEN)
    expect(JSON.parse(String(init!.body))).toMatchObject({
      organization: { name: 'Mercado Bom Preço', document: '12.ABC.345/01DE-35', document_type: 'CNPJ', phone: '11999998888', city: 'Campinas', state: 'SP', segment_ids: ['seg-1'] },
      user: { full_name: 'Ana Lima', password: 'Segura2026xyz' },
      consents: { terms_template_id: 'tpl-1', accept_terms: true, terms_version: '2026-10-01', privacy_version: '2026-10-01', accept_privacy: true, marketing_opt_in: false },
    })

    expect(await screen.findByText('Conta de anunciante criada')).toBeInTheDocument()
    expect(tokenStore.get()).toBe('acc-novo')
    await waitFor(() => expect(useAuthStore.getState().status).toBe('authenticated'))

    await user.click(screen.getByRole('button', { name: 'Falar com o comercial' }))
    await user.click(screen.getByRole('button', { name: 'Pedir contato do comercial' }))
    await waitFor(() => expect(callsTo(f, '/advertiser-signup/sales-contact', 'POST')).toHaveLength(1))
    const [, sInit] = callsTo(f, '/advertiser-signup/sales-contact', 'POST')[0]
    expect(JSON.parse(String(sInit!.body))).toEqual({ org_id: 'org-9', message: null })
    expect(headersOf(sInit as RequestInit).Authorization).toBe('Bearer acc-novo')
    expect(await screen.findByText('O comercial vai entrar em contato.')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Criar minha campanha agora' }))
    expect(await screen.findByText('nova campanha')).toBeInTheDocument()
  })

  it('erros do servidor (§15.4) voltam à etapa e ao campo certos, sem perder o link', async () => {
    const user = userEvent.setup()
    let attempt = 0
    const f = routeFetch([
      { path: '/advertiser-signup/terms', reply: () => TERMS },
      { path: '/advertiser-signup/options', reply: () => OPTIONS },
      { path: '/advertiser-signup/application', reply: () => ({ email: 'ana@exemplo.invalid', expires_at: '2026-10-08T12:00:00Z' }) },
      {
        method: 'POST',
        path: '/advertiser-signup/complete',
        reply: () =>
          ++attempt === 1
            ? apiError(409, 'DOCUMENT_ALREADY_REGISTERED', 'Este CPF/CNPJ já possui cadastro. Fale com o comercial.')
            : apiError(400, 'PASSWORD_POLICY', 'Senha fraca.', { errors: ['A senha deve conter letras e números.'] }),
      },
    ])
    renderSignup(`/cadastro-anunciante?token=${TOKEN}`)
    await fillValid(user)
    await user.click(await screen.findByRole('button', { name: 'Criar conta de anunciante' }))
    expect(await screen.findByRole('heading', { level: 2, name: 'Empresa e contato' })).toBeInTheDocument()
    expect(screen.getAllByText('Este CPF/CNPJ já possui cadastro. Fale com o comercial.').length).toBeGreaterThan(0)
    expect(screen.getByLabelText(/^CNPJ/)).toHaveAttribute('aria-invalid', 'true')
    // nova tentativa com o MESMO link (o servidor só consome o token no sucesso)
    await user.click(screen.getByRole('button', { name: /Revisão/ }))
    await user.click(await screen.findByRole('button', { name: 'Criar conta de anunciante' }))
    expect(await screen.findByRole('heading', { level: 2, name: 'Seu acesso' })).toBeInTheDocument()
    expect(screen.getByText('A senha deve conter letras e números.')).toBeInTheDocument()
    expect(callsTo(f, '/advertiser-signup/complete', 'POST')).toHaveLength(2)
    callsTo(f, '/advertiser-signup/complete', 'POST').forEach(([, init]) =>
      expect(headersOf(init as RequestInit)['X-RD-Signup-Token']).toBe(TOKEN),
    )
    expect(tokenStore.get()).toBeNull()
  })

  it('termos não publicados: avisa e não permite concluir (nenhum dado é enviado)', async () => {
    const f = routeFetch([
      { path: '/advertiser-signup/terms', reply: () => ({ ...TERMS, advertiser_terms: null }) },
      { path: '/advertiser-signup/options', reply: () => OPTIONS },
      { path: '/advertiser-signup/application', reply: () => ({ email: 'ana@exemplo.invalid', expires_at: '2026-10-08T12:00:00Z' }) },
    ])
    const user = userEvent.setup()
    renderSignup(`/cadastro-anunciante?token=${TOKEN}`)
    await user.click(await screen.findByRole('button', { name: /Revisão/ }))
    expect(await screen.findByText(/Conclusão indisponível/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Criar conta de anunciante' })).toBeDisabled()
    expect(callsTo(f, '/advertiser-signup/complete', 'POST')).toHaveLength(0)
  })

  it('link inválido/expirado → aviso + novo pedido de link', async () => {
    routeFetch([
      { path: '/advertiser-signup/terms', reply: () => TERMS },
      { path: '/advertiser-signup/options', reply: () => OPTIONS },
      { path: '/advertiser-signup/application', reply: () => apiError(401, 'SIGNUP_TOKEN_INVALID', 'Link de cadastro inválido, expirado ou já utilizado.') },
    ])
    renderSignup(`/cadastro-anunciante?token=${TOKEN}`)
    expect(await screen.findByText('Link inválido, expirado ou já utilizado')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Enviar link de cadastro' })).toBeInTheDocument()
  })
})

describe('auto-cadastro: telefone com DDD (dado exigido no pagamento — AÇÃO 004)', () => {
  it('8 dígitos sem DDD não passa da etapa "Empresa e contato"', async () => {
    const user = userEvent.setup()
    routeFetch([
      { path: '/advertiser-signup/terms', reply: () => TERMS },
      { path: '/advertiser-signup/options', reply: () => OPTIONS },
      { path: '/advertiser-signup/application', reply: () => ({ email: 'ana@exemplo.invalid', expires_at: '2026-10-08T12:00:00Z' }) },
    ])
    renderSignup(`/cadastro-anunciante?token=${TOKEN}`)
    await screen.findByRole('heading', { level: 2, name: 'Empresa e contato' })
    const type = (re: RegExp, value: string) => fireEvent.change(screen.getByLabelText(re), { target: { value } })
    type(/^CNPJ/, '12.ABC.345/01DE-35')
    type(/Razão social/, 'Mercado Bom Preço')
    type(/^Telefone/, '9999-8888')
    await user.click(screen.getByRole('combobox'))
    await user.click(await screen.findByRole('option', { name: 'SP — São Paulo' }))
    type(/^Cidade/, 'Campinas')
    await user.click(screen.getByRole('button', { name: 'Continuar' }))
    expect(await screen.findByText(/Use \(DD\) NNNN-NNNN para fixo/)).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'Empresa e contato' })).toBeInTheDocument()
  })
})
