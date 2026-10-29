/**
 * Aprovações nos dois modos (SPEC AÇÃO 003 §11.8): ÚNICA → "Autorizar" (solicitante) em vez de "Aprovar",
 * rótulo "Aprovação única", sem rejeição e sem menção a outro Master; alerta forte quando o pedido muda o
 * modo; criar → autorizar no mesmo diálogo; 409 SECOND_MASTER_REQUIRED com "Configurar agora"; DUPLA inalterada.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ApprovalsPage from './ApprovalsPage'
import { ExpenseDialog } from './expense-forms'
import SettingsPage from '@/pages/admin/settings/SettingsPage'
import { useAuthStore } from '@/stores/authStore'
import { useContextStore } from '@/stores/contextStore'
import { tokenStore } from '@/lib/http'
import { fetchRouter, page, type FetchRoute } from '@/components/finance/testing'
import { MASTER_ID, makeMasterMe } from '@/test/securityFixtures'
import { apiError, mockApi, jsonResponse as adminJson, renderRoute as adminRender } from '@/test/adminTestUtils'
import { jsonResponse, renderWithProviders } from '@/test/utils'
import type { ApprovalRequest } from './api'

vi.setConfig({ testTimeout: 30_000 })

const OTHER = 'bbbbbbbb-0000-4000-8000-000000000002'
const SEC_OK = { totp_enabled: true, financial_password_set: true, financial_password_changed_at: null }

function req(partial: Partial<ApprovalRequest>): ApprovalRequest {
  return {
    id: 'r1',
    action_type: 'EXPENSE_CREATE',
    action_label: 'despesa manual',
    payload: { description: 'Internet', amount_cents: 15000, category: 'internet', competence_date: '2026-10-01' },
    payload_sha256: 'a'.repeat(64),
    amount_cents: 15000,
    reason: 'Conta de internet do escritório',
    requested_by: MASTER_ID,
    requested_by_name: 'Sócio A',
    requested_at: '2026-10-07T12:00:00Z',
    status: 'PENDING',
    approved_by: null,
    approved_at: null,
    rejected_by: null,
    rejection_reason: null,
    expires_at: '2026-10-08T12:00:00Z',
    execution_state: 'NOT_STARTED',
    execution_ref: null,
    execution_error: null,
    executed_at: null,
    requires_email_code: true,
    can_approve: false,
    approval_mode: 'SINGLE',
    can_authorize: true,
    is_mode_change: false,
    ...partial,
  }
}

const MINE_SINGLE = req({ id: 'mine' })
const MODE_CHANGE = req({
  id: 'mode',
  action_type: 'APPROVAL_MODE_CHANGE',
  action_label: 'mudança do modo de aprovação financeira',
  payload: { target_mode: 'DUAL', from_mode: 'SINGLE', expected_version: 1 },
  amount_cents: null,
  is_mode_change: true,
})
const THEIRS_DUAL = req({ id: 'theirs', requested_by: OTHER, requested_by_name: 'Sócio B', approval_mode: 'DUAL', can_approve: true, can_authorize: false })

function routes(list: ApprovalRequest[], extra: FetchRoute[] = []) {
  return fetchRouter([
    ...extra,
    { match: /\/financial\/security\/status/, reply: () => SEC_OK },
    { match: /\/financial\/approvals\/[a-z]+\/events/, reply: () => ({ items: [] }) },
    { match: /\/financial\/approvals\/(mine|theirs|mode)$/, reply: (c) => list.find((r) => c.url.endsWith(`/${r.id}`)) },
    { method: 'POST', match: /\/financial\/approvals\/[a-z]+\/email-code/, reply: () => ({ sent: true, expires_in: 600 }) },
    { method: 'POST', match: /\/financial\/approvals\/[a-z]+\/authorize/, reply: (c) => ({ ...list.find((r) => c.url.includes(`/${r.id}/`)), status: 'APPROVED', execution_state: 'EXECUTED' }) },
    { match: /\/financial\/approvals\?/, reply: () => page(list) },
  ])
}

beforeEach(() => {
  tokenStore.clear()
  useContextStore.getState().reset()
})
afterEach(() => vi.unstubAllGlobals())

describe('Aprovações — modo ÚNICA', () => {
  it('lista: "Autorizar" no lugar de "Aprovar", rótulo "Aprovação única", alerta de autorização, nada de outro Master', async () => {
    const me = makeMasterMe('SINGLE')
    useAuthStore.setState({ status: 'authenticated', me, demo: false })
    useContextStore.getState().syncFromMe(me.contexts)
    routes([MINE_SINGLE, MODE_CHANGE])
    renderWithProviders(<ApprovalsPage />)
    expect(await screen.findByRole('button', { name: /Revisar pedido: despesa manual/ })).toHaveTextContent('Revisar e autorizar')
    expect(await screen.findByText(/2 pedidos aguardam a sua autorização/)).toBeInTheDocument()
    expect(screen.getAllByText('Aprovação única').length).toBeGreaterThan(0)
    expect(screen.getByText('Muda o modo de aprovação')).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/outro Admin Master|outro Administrador Master|segundo (Admin|Master)|dois Admin/i)
    expect(screen.queryByRole('button', { name: /Aprovar pedido/ })).not.toBeInTheDocument()
  })

  it('detalhe: o solicitante AUTORIZA com TOTP + senha financeira + código por e-mail (rota /authorize), sem rejeitar nem "Aprovar"', async () => {
    const user = userEvent.setup()
    const me = makeMasterMe('SINGLE')
    useAuthStore.setState({ status: 'authenticated', me, demo: false })
    useContextStore.getState().syncFromMe(me.contexts)
    const r = routes([MINE_SINGLE])
    renderWithProviders(<ApprovalsPage />)
    await user.click(await screen.findByRole('button', { name: /Revisar pedido: despesa manual/ }))
    const sheet = await screen.findByRole('dialog')
    const s = within(sheet)
    expect(await s.findByRole('form', { name: 'Autorizar operação' })).toBeInTheDocument()
    expect(s.queryByRole('form', { name: 'Aprovar pedido' })).not.toBeInTheDocument()
    expect(s.queryByRole('button', { name: 'Aprovar pedido' })).not.toBeInTheDocument()
    expect(s.queryByRole('button', { name: 'Rejeitar pedido' })).not.toBeInTheDocument() // no modo único o solicitante cancela
    expect(s.queryByText('Você é o solicitante')).not.toBeInTheDocument()
    expect(s.getByRole('button', { name: 'Cancelar solicitação' })).toBeInTheDocument()
    expect(sheet.textContent).not.toMatch(/outro Admin Master|outro Administrador Master/i)

    await user.click(s.getByRole('button', { name: 'Autorizar operação' }))
    expect(await s.findByText('Informe o código de 6 dígitos do aplicativo autenticador.')).toBeInTheDocument()
    expect(r.find('POST', /\/authorize/)).toHaveLength(0)
    await user.click(s.getByRole('button', { name: 'Enviar código por e-mail' }))
    await waitFor(() => expect(r.find('POST', /\/email-code/)).toHaveLength(1))
    await user.type(s.getByLabelText(/Código do autenticador/), '123456')
    await user.type(s.getByLabelText(/^Senha financeira/), 'financeira-forte-1')
    await user.type(s.getByLabelText(/Código enviado por e-mail/), '654321')
    await user.click(s.getByRole('button', { name: 'Autorizar operação' }))
    await waitFor(() => expect(r.find('POST', /\/authorize/)).toHaveLength(1))
    expect(r.find('POST', /\/authorize/)[0].body).toEqual({ totp_code: '123456', financial_password: 'financeira-forte-1', payload_sha256: 'a'.repeat(64), email_code: '654321' })
    expect(r.find('POST', /\/approve/)).toHaveLength(0)
  })

  it('pedido que muda o modo: alerta forte no detalhe, com o novo modo e as consequências', async () => {
    const user = userEvent.setup()
    const me = makeMasterMe('SINGLE')
    useAuthStore.setState({ status: 'authenticated', me, demo: false })
    useContextStore.getState().syncFromMe(me.contexts)
    routes([MODE_CHANGE])
    renderWithProviders(<ApprovalsPage />)
    await user.click(await screen.findByRole('button', { name: /Revisar pedido: mudança do modo/ }))
    const sheet = await screen.findByRole('dialog')
    const alert = await within(sheet).findByTestId('mode-change-alert')
    expect(alert).toHaveTextContent('Esta aprovação altera o modo de aprovação financeira da instância')
    expect(alert).toHaveTextContent('Mudança de Aprovação única para Aprovação dupla')
    expect(alert).toHaveTextContent('Os pedidos pendentes serão cancelados')
    expect(within(sheet).getByText('Novo modo de aprovação')).toBeInTheDocument()
  })

  it('erro do servidor fica no formulário: o modo mudou (409 APPROVAL_MODE_CHANGED) e o TOTP é limpo', async () => {
    const user = userEvent.setup()
    const me = makeMasterMe('SINGLE')
    useAuthStore.setState({ status: 'authenticated', me, demo: false })
    useContextStore.getState().syncFromMe(me.contexts)
    routes([MINE_SINGLE], [
      {
        method: 'POST',
        match: /\/financial\/approvals\/mine\/authorize/,
        reply: () => jsonResponse({ error: { code: 'APPROVAL_MODE_CHANGED', message: 'O modo de aprovação mudou; crie uma nova solicitação.' } }, 409),
      },
    ])
    renderWithProviders(<ApprovalsPage />)
    await user.click(await screen.findByRole('button', { name: /Revisar pedido: despesa manual/ }))
    const s = within(await screen.findByRole('dialog'))
    await user.type(await s.findByLabelText(/Código do autenticador/), '123456')
    await user.type(s.getByLabelText(/^Senha financeira/), 'financeira-forte-1')
    await user.type(s.getByLabelText(/Código enviado por e-mail/), '654321')
    await user.click(s.getByRole('button', { name: 'Autorizar operação' }))
    expect(await s.findByText('O modo de aprovação mudou; crie uma nova solicitação.')).toBeInTheDocument()
    expect(s.getByLabelText(/Código do autenticador/)).toHaveValue('') // TOTP de uso único
  })

  it('?pedido=<id> abre o detalhe do pedido (atalho vindo da tela de Segurança)', async () => {
    const me = makeMasterMe('SINGLE')
    useAuthStore.setState({ status: 'authenticated', me, demo: false })
    useContextStore.getState().syncFromMe(me.contexts)
    routes([MODE_CHANGE])
    renderWithProviders(<ApprovalsPage />, { route: '/financeiro/aprovacoes?pedido=mode' })
    expect(await screen.findByTestId('mode-change-alert')).toBeInTheDocument()
  })
})

describe('Aprovações — modo DUPLA (inalterado)', () => {
  it('o outro Master continua vendo "Aprovar pedido" e "Rejeitar"; nada de "Autorizar"', async () => {
    const user = userEvent.setup()
    const me = makeMasterMe('DUAL', 'DONE')
    useAuthStore.setState({ status: 'authenticated', me, demo: false })
    useContextStore.getState().syncFromMe(me.contexts)
    routes([THEIRS_DUAL])
    renderWithProviders(<ApprovalsPage />)
    expect(await screen.findByText(/1 pedido aguarda a sua aprovação/)).toBeInTheDocument()
    await user.click(await screen.findByRole('button', { name: /Revisar pedido: despesa manual/ }))
    const s = within(await screen.findByRole('dialog'))
    expect(await s.findByRole('form', { name: 'Aprovar pedido' })).toBeInTheDocument()
    expect(s.getByRole('button', { name: 'Rejeitar pedido' })).toBeInTheDocument()
    expect(s.queryByRole('form', { name: 'Autorizar operação' })).not.toBeInTheDocument()
    expect(screen.queryByText('Aprovação única')).not.toBeInTheDocument()
  })
})

describe('criar → autorizar no mesmo diálogo (ÚNICA)', () => {
  function openExpense(mode: 'SINGLE' | 'DUAL') {
    const me = mode === 'SINGLE' ? makeMasterMe('SINGLE') : makeMasterMe('DUAL', 'DONE')
    useAuthStore.setState({ status: 'authenticated', me, demo: false })
    useContextStore.getState().syncFromMe(me.contexts)
    const onOpenChange = vi.fn()
    renderWithProviders(<ExpenseDialog open onOpenChange={onOpenChange} payable={null} />)
    return onOpenChange
  }
  async function fillExpense(user: ReturnType<typeof userEvent.setup>) {
    const dialog = await screen.findByRole('dialog')
    const d = within(dialog)
    await user.type(d.getByLabelText(/^Descrição/), 'Internet do escritório')
    await user.type(d.getByLabelText(/^Categoria/), 'internet')
    await user.type(d.getByLabelText(/^Valor/), '150,00')
    await user.type(d.getByLabelText(/^Motivo/), 'Conta de internet do escritório')
    await user.click(d.getByRole('button', { name: /Criar pedido e autorizar|Solicitar aprovação/ }))
    return d
  }

  it('ÚNICA: depois de criar, o MESMO diálogo vira a autorização (3 fatores) e, ao autorizar, fecha', async () => {
    const user = userEvent.setup()
    const created = req({ id: 'exp1' })
    const r = fetchRouter([
      { match: /\/financial\/security\/status/, reply: () => SEC_OK },
      { method: 'POST', match: /\/financial\/payables$/, reply: () => created },
      { method: 'POST', match: /\/financial\/approvals\/exp1\/email-code$/, reply: () => ({ sent: true, expires_in: 600 }) },
      { method: 'POST', match: /\/financial\/approvals\/exp1\/authorize$/, reply: () => ({ ...created, status: 'APPROVED', execution_state: 'EXECUTED' }) },
    ])
    const onOpenChange = openExpense('SINGLE')
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent('depois que você autorizar o pedido')
    expect(dialog).not.toHaveTextContent(/outro Admin Master/)
    const d = await fillExpense(user)
    expect(await d.findByRole('heading', { name: 'Autorizar operação' })).toBeInTheDocument() // mesmo diálogo
    expect(screen.getAllByRole('dialog')).toHaveLength(1)
    expect(d.getByText('Despesa manual')).toBeInTheDocument()
    expect(onOpenChange).not.toHaveBeenCalled()
    await user.click(d.getByRole('button', { name: 'Enviar código por e-mail' }))
    await user.type(d.getByLabelText(/Código do autenticador/), '123456')
    await user.type(d.getByLabelText(/^Senha financeira/), 'financeira-forte-1')
    await user.type(d.getByLabelText(/Código enviado por e-mail/), '654321')
    await user.click(d.getByRole('button', { name: 'Autorizar operação' }))
    await waitFor(() => expect(r.find('POST', /exp1\/authorize$/)).toHaveLength(1))
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))
  })

  it('DUPLA: o pedido segue para o outro Master e o diálogo fecha (sem etapa de autorização)', async () => {
    const user = userEvent.setup()
    const created = req({ id: 'exp2', approval_mode: 'DUAL', can_authorize: false })
    fetchRouter([
      { match: /\/financial\/security\/status/, reply: () => SEC_OK },
      { method: 'POST', match: /\/financial\/payables$/, reply: () => created },
    ])
    const onOpenChange = openExpense('DUAL')
    await fillExpense(user)
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))
    expect(screen.queryByRole('heading', { name: 'Autorizar operação' })).not.toBeInTheDocument()
  })

  it('409 SECOND_MASTER_REQUIRED na criação: mensagem do servidor + "Configurar agora"', async () => {
    const user = userEvent.setup()
    fetchRouter([
      { match: /\/financial\/security\/status/, reply: () => SEC_OK },
      {
        method: 'POST',
        match: /\/financial\/payables$/,
        reply: () =>
          jsonResponse(
            { error: { code: 'SECOND_MASTER_REQUIRED', message: 'Esta instalação usa dupla aprovação financeira. Cadastre e ative o segundo Administrador Master para liberar esta operação.' } },
            409,
          ),
      },
    ])
    openExpense('DUAL')
    const d = await fillExpense(user)
    expect(await d.findByText(/Cadastre e ative o segundo Administrador Master para liberar esta operação/)).toBeInTheDocument()
    expect(d.getByRole('link', { name: 'Configurar agora' })).toHaveAttribute('href', '/configuracoes/seguranca')
  })
})

describe('configuração financeira na ÚNICA (SettingsPage)', () => {
  it('cria o pedido SETTING_CHANGE e autoriza ali mesmo, sem mandar ninguém ao "outro Master"', async () => {
    const user = userEvent.setup()
    const me = makeMasterMe('SINGLE')
    useAuthStore.setState({ status: 'authenticated', me, demo: false })
    useContextStore.getState().syncFromMe(me.contexts)
    const POOL = { key: 'commission.pool_bps', value: 500, default: 500, type: 'int', choices: null, min: 0, max: 10000, financial: true, category: 'financeiro', description: 'Pool de comissão em basis points (500 = 5%).', version: 0, updated_at: null, updated_by: null }
    const created = req({ id: 'set1', action_type: 'SETTING_CHANGE', action_label: 'alteração de configuração financeira', payload: { key: 'commission.pool_bps', value: 600, expected_version: 0 } })
    const { calls } = mockApi((c) => {
      if (c.path === '/settings') return adminJson({ items: [POOL] })
      if (c.path === '/financial/security/status') return adminJson(SEC_OK)
      if (c.method === 'POST' && c.path === '/financial/approvals') return adminJson(created, 201)
      if (c.method === 'POST' && c.path === '/financial/approvals/set1/email-code') return adminJson({ sent: true, expires_in: 600 })
      if (c.method === 'POST' && c.path === '/financial/approvals/set1/authorize') return adminJson({ ...created, status: 'APPROVED', execution_state: 'EXECUTED' })
      if (c.path === '/security/approval-mode') return apiError(404, 'NOT_FOUND', 'x')
      return undefined
    })
    adminRender(<SettingsPage />, '/configuracoes', '/configuracoes')
    await screen.findByText(POOL.description)
    const row = screen.getByRole('heading', { name: /Pool de comissão/ }).closest('section') as HTMLElement
    const w = within(row)
    expect(w.getByText('Financeira · aprovação única')).toBeInTheDocument()
    expect(screen.getByText('Chaves financeiras exigem autorização')).toBeInTheDocument()
    const input = w.getByLabelText('Novo valor')
    await user.clear(input)
    await user.type(input, '600')
    await user.type(w.getByLabelText(/Motivo do pedido/), 'Ajuste do pool aprovado em reunião')
    await user.click(w.getByRole('button', { name: /Solicitar alteração \(aprovação única\)/ }))
    expect(await w.findByText('Pedido criado — falta a sua autorização')).toBeInTheDocument()
    await user.type(w.getByLabelText(/Código do autenticador/), '123456')
    await user.type(w.getByLabelText(/^Senha financeira/), 'financeira-forte-1')
    await user.type(w.getByLabelText(/Código enviado por e-mail/), '654321')
    await user.click(w.getByRole('button', { name: 'Autorizar operação' }))
    await waitFor(() => expect(calls.some((c) => c.path === '/financial/approvals/set1/authorize')).toBe(true))
    expect(document.body.textContent).not.toMatch(/outro Admin Master|dupla aprovação dos Admin Master/i)
  })
})

