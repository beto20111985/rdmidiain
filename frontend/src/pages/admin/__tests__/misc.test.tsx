import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router-dom'
import { documentProblem, formatDocument, isValidCnpj, isValidCpf } from '@/components/admin/documents'
import { parseBrlToCents, centsToInput } from '@/components/admin/money'
import { nextDayStartUtc, startOfDayUtc } from '@/lib/dates'
import { brandColorProblems, contrastRatio } from '../whitelabel/contrast'
import OrganizationNewPage from '../organizations/OrganizationNewPage'
import AcceptInvitePage from '@/features/auth/pages/AcceptInvitePage'
import { APP_ROUTES } from '@/app/routes'
import { renderWithProviders } from '@/test/utils'
import { jsonResponse, mockApi, resetSession, signInAs, withPageMeta } from '@/test/adminTestUtils'

// Páginas completas com muitos campos: folga para execução paralela com as demais suítes.
vi.setConfig({ testTimeout: 20_000 })

beforeEach(() => resetSession())
afterEach(() => vi.unstubAllGlobals())

describe('CPF/CNPJ (inclusive CNPJ alfanumérico)', () => {
  it('valida dígitos verificadores', () => {
    expect(isValidCpf('529.982.247-25')).toBe(true)
    expect(isValidCpf('529.982.247-24')).toBe(false)
    expect(isValidCpf('111.111.111-11')).toBe(false)
    expect(isValidCnpj('11.222.333/0001-81')).toBe(true)
    expect(isValidCnpj('11.222.333/0001-80')).toBe(false)
    // Exemplo oficial da Receita Federal (IN RFB 2.229/2024)
    expect(isValidCnpj('12.ABC.345/01DE-35')).toBe(true)
    expect(isValidCnpj('12.ABC.345/01DE-36')).toBe(false)
    expect(formatDocument('12abc34501de35', 'CNPJ')).toBe('12.ABC.345/01DE-35')
    expect(documentProblem('123', 'CPF')).toBe('O CPF tem 11 dígitos.')
  })

  it('o formulário de nova empresa bloqueia CNPJ inválido e envia o documento normalizado com papéis combinados', async () => {
    const user = userEvent.setup()
    signInAs({ permissions: ['organizations.view', 'organizations.manage'] })
    const { calls } = mockApi((c) =>
      c.method === 'POST' && c.path === '/organizations' ? jsonResponse({ id: 'o9', name: 'Mercado Teste', trade_name: null }, 201) : undefined,
    )
    renderWithProviders(
      withPageMeta(
        <Routes>
          <Route path="/empresas/nova" element={<OrganizationNewPage />} />
          <Route path="/empresas/:id" element={<p>detalhe da empresa</p>} />
        </Routes>,
      ),
      { route: '/empresas/nova' },
    )
    await user.type(screen.getByLabelText(/^CNPJ/), '12.ABC.345/01DE-36')
    await user.type(screen.getByLabelText(/Razão social/), 'Mercado Teste')
    await user.click(screen.getByRole('button', { name: 'Cadastrar empresa' }))
    expect(await screen.findByText(/CNPJ inválido/)).toBeInTheDocument()
    expect(screen.getByText('Escolha ao menos um papel.')).toBeInTheDocument()
    expect(calls.some((c) => c.method === 'POST')).toBe(false)

    await user.clear(screen.getByLabelText(/^CNPJ/))
    await user.type(screen.getByLabelText(/^CNPJ/), '12.ABC.345/01DE-35')
    await user.click(screen.getByRole('checkbox', { name: 'Anunciante' }))
    await user.click(screen.getByRole('checkbox', { name: 'Parceiro (estabelecimento)' }))
    // AÇÃO 004: anunciante exige os dados do pagador (e-mail + telefone com DDD)
    await user.type(screen.getByLabelText(/^E-mail/), 'contato@mercadoteste.com.br')
    await user.type(screen.getByLabelText(/^Telefone/), '62999990000')
    await user.click(screen.getByRole('button', { name: 'Cadastrar empresa' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true))
    expect(calls.find((c) => c.method === 'POST')!.body).toMatchObject({
      document: '12ABC34501DE35',
      document_type: 'CNPJ',
      name: 'Mercado Teste',
      business_roles: ['ADVERTISER', 'PARTNER'],
      email: 'contato@mercadoteste.com.br',
      phone: '(62) 99999-0000',
    })
    expect(await screen.findByText('detalhe da empresa')).toBeInTheDocument()
  })
})

describe('utilidades', () => {
  // A proteção do Radix Select ("" e valor repetido) foi consolidada na fundação: src/components/ui/select.test.tsx.

  it('BRL → centavos inteiros (nunca float)', () => {
    expect(parseBrlToCents('1.234,56')).toBe(123456)
    expect(parseBrlToCents('10')).toBe(1000)
    expect(parseBrlToCents('0,5')).toBe(50)
    expect(parseBrlToCents('abc')).toBeNull()
    expect(centsToInput(15000)).toBe('150,00')
  })

  it('período da auditoria no fuso de São Paulo (fim exclusivo no dia seguinte)', () => {
    expect(startOfDayUtc('2026-10-07')).toBe('2026-10-07T03:00:00Z')
    expect(nextDayStartUtc('2026-10-07')).toBe('2026-10-08T03:00:00Z')
    expect(startOfDayUtc('07/10/2026')).toBeNull()
  })

  it('contraste das cores da marca segue o critério do servidor', () => {
    expect(contrastRatio('#0056B3', '#FFFFFF')).toBeGreaterThan(4.5)
    expect(brandColorProblems('#0056B3', '#0B84FF')).toEqual({ primary: null, accent: null })
    expect(brandColorProblems('#5BCEFF', '#082A4A').primary).toMatch(/mínimo 4,5:1/)
    expect(brandColorProblems('#0056B3', '#082A4A').accent).toMatch(/mínimo 3:1/)
  })
})

describe('Convite por link do e-mail', () => {
  it('aceita /convite?token=… (formato enviado pelo backend)', async () => {
    const user = userEvent.setup()
    const { calls } = mockApi((c) => (c.path === '/auth/invites/accept' ? jsonResponse({ error: { code: 'INVITE_INVALID', message: 'Convite inválido ou expirado.' } }, 400) : undefined))
    renderWithProviders(
      <Routes>
        <Route path="/convite/:token" element={<AcceptInvitePage />} />
        <Route path="/convite" element={<AcceptInvitePage />} />
      </Routes>,
      { route: '/convite?token=tok_abcdefghijklmnopqrstuvwxyz012345' },
    )
    expect(screen.queryByText(/link do convite está incompleto/)).not.toBeInTheDocument()
    await user.type(screen.getByLabelText(/Nome completo/), 'Pessoa Convidada')
    const pw = screen.getAllByLabelText(/senha/i).filter((el) => el.tagName === 'INPUT')
    for (const el of pw) await user.type(el, 'SenhaForte2026x')
    await user.click(screen.getByRole('button', { name: 'Criar acesso' }))
    await waitFor(() => expect(calls.some((c) => c.path === '/auth/invites/accept')).toBe(true))
    expect(calls.find((c) => c.path === '/auth/invites/accept')!.body).toMatchObject({ token: 'tok_abcdefghijklmnopqrstuvwxyz012345' })
  })

  it('sem token avisa que o link está incompleto', () => {
    mockApi(() => undefined)
    renderWithProviders(
      <Routes>
        <Route path="/convite" element={<AcceptInvitePage />} />
      </Routes>,
      { route: '/convite' },
    )
    expect(screen.getByText(/link do convite está incompleto/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Criar acesso' })).toBeDisabled()
  })
})

describe('rotas do FE-ADM', () => {
  it('todas as páginas do escopo estão implementadas (sem NotImplementedYet)', () => {
    const ids = [
      'rd.organizations', 'rd.organizations.new', 'rd.organizations.detail', 'rd.users', 'rd.users.detail', 'rd.roles', 'rd.audit',
      'rd.settings', 'rd.settings.whitelabel', 'rd.settings.lgpd', 'rd.controlplane', 'rd.controlplane.rollout', 'common.notifications',
      'common.privacy', 'rd.support', 'rd.support.new', 'rd.support.detail', 'est.support', 'est.support.new', 'est.support.detail',
      'adv.support', 'adv.support.new', 'adv.support.detail', 'rd.design.briefings', 'rd.design.new', 'rd.design.detail', 'rd.design.queue',
      'rd.design.versions', 'rd.design.templates', 'rd.design.templates.new', 'rd.design.templates.detail', 'adv.design', 'adv.design.new',
      'adv.design.detail', 'est.design', 'est.design.new', 'est.design.detail',
    ]
    for (const id of ids) {
      const r = APP_ROUTES.find((x) => x.id === id)
      expect(r, id).toBeDefined()
      expect(typeof r!.load, id).toBe('function')
    }
  })
})
