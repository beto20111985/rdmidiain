/**
 * Configurações → Segurança da instância (SPEC AÇÃO 003 §11.7): "Aprovação financeira: Única | Dupla" SEM
 * switch; mudança por diálogo guiado (consequências, motivo ≥ 20 e fatores); SINGLE sem nenhuma menção a
 * segundo Master; gestão do convite só na DUPLA; estados de carregamento/erro/sem permissão.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import SecuritySettingsPage from '../settings/SecuritySettingsPage'
import { useAuthStore } from '@/stores/authStore'
import { useContextStore } from '@/stores/contextStore'
import { tokenStore } from '@/lib/http'
import { fetchRouter, type FetchRoute } from '@/components/finance/testing'
import { MASTER_ID, makeMasterMe } from '@/test/securityFixtures'
import { jsonResponse, makeMe, renderWithProviders } from '@/test/utils'
import type { ApprovalModeInfo, MastersInfo } from '@/features/security/types'
import type { Me } from '@/features/auth/types'
import type { ApprovalRequest } from '@/pages/finance/api'

const SEC_OK = { totp_enabled: true, financial_password_set: true, financial_password_changed_at: null }

function info(partial: Partial<ApprovalModeInfo> = {}): ApprovalModeInfo {
  return {
    mode: 'SINGLE',
    mode_version: 1,
    initialized_at: '2026-10-08T12:00:00Z',
    initialized_source: 'BOOTSTRAP_ENV',
    dual_approval_ready: null, // contrato: null no modo SINGLE
    active_masters: 1,
    pending_change_request: null,
    history: [
      { mode_version: 1, kind: 'INITIALIZED', from_mode: null, to_mode: 'SINGLE', source: 'BOOTSTRAP_ENV', requested_by_name: null, authorized_by_name: null, reason: null, approval_request_id: null, created_at: '2026-10-08T12:00:00Z' },
    ],
    integrity: { ok: true },
    env: { divergence: false, unsupported_var_present: false },
    ...partial,
  }
}

const MASTERS_DUAL_PENDING: MastersInfo = {
  approval_mode: 'DUAL',
  seats: [
    { seat: 1, name: 'Sócio A', status: 'ACTIVE', is_self: true },
    { seat: 2, name: null, status: 'VACANT', is_self: false },
  ],
  invite: null,
  can_invite: true,
}

function approval(partial: Partial<ApprovalRequest>): ApprovalRequest {
  return {
    id: 'req-mode',
    action_type: 'APPROVAL_MODE_CHANGE',
    action_label: 'mudança do modo de aprovação financeira',
    payload: { target_mode: 'DUAL', from_mode: 'SINGLE', expected_version: 1 },
    payload_sha256: 'c'.repeat(64),
    amount_cents: null,
    reason: 'Entrada do segundo sócio na operação financeira',
    requested_by: MASTER_ID,
    requested_by_name: 'Sócio A',
    requested_at: '2026-10-08T13:00:00Z',
    status: 'PENDING',
    approved_by: null,
    approved_at: null,
    rejected_by: null,
    rejection_reason: null,
    expires_at: '2026-10-09T13:00:00Z',
    execution_state: 'NOT_STARTED',
    execution_ref: null,
    execution_error: null,
    executed_at: null,
    requires_email_code: true,
    can_approve: false,
    approval_mode: 'SINGLE',
    can_authorize: true,
    is_mode_change: true,
    ...partial,
  }
}

function signIn(me: Me) {
  useAuthStore.setState({ status: 'authenticated', me, demo: false })
  useContextStore.getState().syncFromMe(me.contexts)
}

function base(routes: FetchRoute[]) {
  return fetchRouter([{ match: /\/financial\/security\/status$/, reply: () => SEC_OK }, ...routes])
}

async function openDialog(user: ReturnType<typeof userEvent.setup>, label: RegExp) {
  await user.click(await screen.findByRole('button', { name: label }))
  return screen.findByRole('dialog')
}

beforeEach(() => {
  tokenStore.clear()
  useContextStore.getState().reset()
})
afterEach(() => vi.unstubAllGlobals())

describe('Segurança da instância — aprovação ÚNICA', () => {
  it('mostra "Única | Dupla" SEM switch, sem lista de assentos e sem NENHUMA menção a segundo Master', async () => {
    signIn(makeMasterMe('SINGLE'))
    const r = base([{ match: /\/security\/approval-mode$/, reply: () => info() }])
    renderWithProviders(<SecuritySettingsPage />)
    expect(await screen.findByText('Aprovação financeira')).toBeInTheDocument()
    expect(screen.getByRole('group', { name: 'Modo atual: Única' })).toBeInTheDocument()
    expect(screen.getByText('Única').closest('[aria-current]')).toHaveAttribute('aria-current', 'true')
    expect(screen.queryByRole('switch')).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Solicitar mudança para Dupla/ })).toBeInTheDocument()
    expect(screen.queryByText('Administradores Master')).not.toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/segundo|2º Master|outro Administrador Master|outro Admin Master/i)
    expect(r.find('GET', /\/security\/masters$/)).toHaveLength(0)
  })

  it('Única → Dupla: diálogo guiado (consequências → motivo ≥ 20 → autorização com 3 fatores); nada de switch', async () => {
    const user = userEvent.setup()
    signIn(makeMasterMe('SINGLE'))
    const created = approval({})
    const r = base([
      { match: /\/security\/approval-mode$/, reply: () => info() },
      { method: 'POST', match: /\/financial\/approvals$/, reply: () => created },
      { method: 'POST', match: /\/financial\/approvals\/req-mode\/email-code$/, reply: () => ({ sent: true, expires_in: 600 }) },
      { method: 'POST', match: /\/financial\/approvals\/req-mode\/authorize$/, reply: () => ({ ...created, status: 'APPROVED', execution_state: 'EXECUTED', can_authorize: false }) },
      { match: /\/me$/, reply: () => makeMasterMe('DUAL', 'NOT_INVITED') },
    ])
    renderWithProviders(<SecuritySettingsPage />)
    const dialog = await openDialog(user, /Solicitar mudança para Dupla/)
    const d = within(dialog)

    // 1. consequências (lista real) e confirmação "Entendi"
    expect(d.getByRole('heading', { name: 'Solicitar mudança para aprovação Dupla' })).toBeInTheDocument()
    expect(d.getByText('O que muda')).toBeInTheDocument()
    expect(d.getByText(/É obrigatório cadastrar e ativar o segundo Administrador Master/)).toBeInTheDocument()
    expect(d.getByText(/Pedidos de aprovação ainda pendentes na aprovação única são cancelados/)).toBeInTheDocument()
    expect(d.getByRole('button', { name: 'Continuar' })).toBeDisabled()
    await user.click(d.getByRole('checkbox', { name: /Entendi as consequências/ }))
    await user.click(d.getByRole('button', { name: 'Continuar' }))

    // 2. motivo ≥ 20 caracteres
    await user.type(await d.findByLabelText(/Motivo da mudança/), 'curto demais')
    await user.click(d.getByRole('button', { name: 'Continuar para a autorização' }))
    expect(await d.findByText(/Explique o motivo \(mínimo 20 caracteres\)/)).toBeInTheDocument()
    expect(r.find('POST', /\/financial\/approvals$/)).toHaveLength(0)
    await user.clear(d.getByLabelText(/Motivo da mudança/))
    await user.type(d.getByLabelText(/Motivo da mudança/), 'Entrada do segundo sócio na operação financeira')
    await user.click(d.getByRole('button', { name: 'Continuar para a autorização' }))

    await waitFor(() => expect(r.find('POST', /\/financial\/approvals$/)).toHaveLength(1))
    const create = r.find('POST', /\/financial\/approvals$/)[0]
    expect(create.body).toEqual({ action_type: 'APPROVAL_MODE_CHANGE', payload: { target_mode: 'DUAL' }, reason: 'Entrada do segundo sócio na operação financeira' })
    expect(create.headers['Idempotency-Key']).toBeTruthy()

    // 3. autorização do próprio Master: TOTP + senha financeira + código por e-mail
    await d.findByText(/Pedido criado\. Para aplicar a mudança, autorize/)
    await user.click(d.getByRole('button', { name: 'Enviar código por e-mail' }))
    await waitFor(() => expect(r.find('POST', /email-code$/)).toHaveLength(1))
    await user.type(d.getByLabelText(/Código do autenticador/), '123456')
    await user.type(d.getByLabelText(/^Senha financeira/), 'financeira-forte-1')
    await user.type(d.getByLabelText(/Código enviado por e-mail/), '654321')
    await user.click(d.getByRole('button', { name: 'Autorizar operação' }))
    await waitFor(() => expect(r.find('POST', /authorize$/)).toHaveLength(1))
    expect(r.find('POST', /authorize$/)[0].body).toEqual({ totp_code: '123456', financial_password: 'financeira-forte-1', payload_sha256: 'c'.repeat(64), email_code: '654321' })
    expect(await d.findByText('Modo de aprovação alterado')).toBeInTheDocument()
    expect(d.getByText(/Modo em vigor: aprovação dupla/)).toBeInTheDocument()
  }, 60_000)
})

describe('Segurança da instância — dupla aprovação', () => {
  it('lista os assentos (nome e estado, sem e-mail), o formulário de convite e o histórico para o Master', async () => {
    signIn(makeMasterMe('DUAL', 'NOT_INVITED'))
    base([
      {
        match: /\/security\/approval-mode$/,
        reply: () =>
          info({
            mode: 'DUAL',
            mode_version: 2,
            dual_approval_ready: false,
            active_masters: 1,
            history: [
              { mode_version: 1, kind: 'INITIALIZED', from_mode: null, to_mode: 'SINGLE', source: 'BOOTSTRAP_ENV', requested_by_name: null, authorized_by_name: null, reason: null, approval_request_id: null, created_at: '2026-10-08T12:00:00Z' },
              { mode_version: 2, kind: 'SINGLE_TO_DUAL', from_mode: 'SINGLE', to_mode: 'DUAL', source: 'APPROVAL_REQUEST', requested_by_name: 'Sócio A', authorized_by_name: 'Sócio A', reason: 'Entrada do segundo sócio na operação', approval_request_id: 'r', created_at: '2026-10-08T14:00:00Z' },
            ],
          }),
      },
      { match: /\/security\/masters$/, reply: () => MASTERS_DUAL_PENDING },
    ])
    renderWithProviders(<SecuritySettingsPage />)
    expect(await screen.findByRole('group', { name: 'Modo atual: Dupla' })).toBeInTheDocument()
    const seats = await screen.findByRole('list', { name: 'Assentos de Administrador Master' })
    expect(within(seats).getByText(/Sócio A \(você\)/)).toBeInTheDocument()
    expect(within(seats).getByText('Ativo')).toBeInTheDocument()
    expect(within(seats).getByText('Vago')).toBeInTheDocument()
    expect(await screen.findByLabelText(/E-mail do convidado/)).toBeInTheDocument()
    // voltar à única exige o outro Master ativo: botão desabilitado com a explicação
    const toSingle = screen.getByRole('button', { name: /Solicitar mudança para Única/ })
    expect(toSingle).toBeDisabled()
    expect(screen.getByText(/o segundo Administrador Master precisa estar ativo/)).toBeInTheDocument()
    // histórico (versão, quem pediu, quem autorizou, motivo)
    await userEvent.setup().click(screen.getByText(/Histórico do modo \(2\)/))
    const hist = screen.getByRole('list', { name: 'Histórico do modo de aprovação' })
    expect(within(hist).getByText(/Dupla → |Única → Dupla/)).toBeInTheDocument()
    expect(within(hist).getByText(/Motivo: Entrada do segundo sócio na operação/)).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/@rd\.invalid/)
  })

  it('Dupla → Única: pede TOTP + senha financeira na criação (step_up) e fica aguardando o outro Master', async () => {
    const user = userEvent.setup()
    signIn(makeMasterMe('DUAL', 'DONE'))
    const created = approval({ payload: { target_mode: 'SINGLE', from_mode: 'DUAL', expected_version: 2 }, approval_mode: 'DUAL', can_authorize: false })
    const r = base([
      { match: /\/security\/approval-mode$/, reply: () => info({ mode: 'DUAL', mode_version: 2, dual_approval_ready: true, active_masters: 2 }) },
      { match: /\/security\/masters$/, reply: () => ({ approval_mode: 'DUAL', seats: [{ seat: 1, name: 'Sócio A', status: 'ACTIVE', is_self: true }, { seat: 2, name: 'Sócio B', status: 'ACTIVE', is_self: false }], invite: null, can_invite: false }) },
      { method: 'POST', match: /\/financial\/approvals$/, reply: () => created },
      { match: /\/me$/, reply: () => makeMasterMe('DUAL', 'DONE') },
    ])
    renderWithProviders(<SecuritySettingsPage />)
    const dialog = await openDialog(user, /Solicitar mudança para Única/)
    const d = within(dialog)
    expect(d.getByText(/Um único Administrador Master passa a autorizar cada operação financeira/)).toBeInTheDocument()
    expect(d.getByText(/só vale depois que o outro Administrador Master aprovar/)).toBeInTheDocument()
    await user.click(d.getByRole('checkbox', { name: /Entendi as consequências/ }))
    await user.click(d.getByRole('button', { name: 'Continuar' }))
    await user.type(await d.findByLabelText(/Motivo da mudança/), 'Sócio deixou a operação financeira da empresa')
    // sem os fatores nada é enviado
    await user.click(d.getByRole('button', { name: 'Solicitar mudança' }))
    expect(await d.findByText('Informe o código de 6 dígitos do aplicativo autenticador.')).toBeInTheDocument()
    expect(r.find('POST', /\/financial\/approvals$/)).toHaveLength(0)
    await user.type(d.getByLabelText(/Código do autenticador/), '123456')
    await user.type(d.getByLabelText(/^Senha financeira/), 'financeira-forte-1')
    await user.click(d.getByRole('button', { name: 'Solicitar mudança' }))
    await waitFor(() => expect(r.find('POST', /\/financial\/approvals$/)).toHaveLength(1))
    expect(r.find('POST', /\/financial\/approvals$/)[0].body).toEqual({
      action_type: 'APPROVAL_MODE_CHANGE',
      payload: { target_mode: 'SINGLE' },
      reason: 'Sócio deixou a operação financeira da empresa',
      step_up: { totp_code: '123456', financial_password: 'financeira-forte-1' },
    })
    expect(await d.findByText('Solicitação enviada')).toBeInTheDocument()
    expect(d.getByText(/Aguardando a aprovação do outro Administrador Master/)).toBeInTheDocument()
    expect(d.getByRole('link', { name: 'Abrir o pedido' })).toHaveAttribute('href', '/financeiro/aprovacoes?pedido=req-mode')
  }, 60_000)

  it('409 SECOND_MASTER_REQUIRED na criação: mensagem do servidor + "Configurar agora" (Master)', async () => {
    const user = userEvent.setup()
    signIn(makeMasterMe('DUAL', 'DONE'))
    base([
      { match: /\/security\/approval-mode$/, reply: () => info({ mode: 'DUAL', mode_version: 2, dual_approval_ready: true }) },
      { match: /\/security\/masters$/, reply: () => MASTERS_DUAL_PENDING },
      {
        method: 'POST',
        match: /\/financial\/approvals$/,
        reply: () =>
          jsonResponse(
            { error: { code: 'SECOND_MASTER_REQUIRED', message: 'Esta instalação usa dupla aprovação financeira. Cadastre e ative o segundo Administrador Master para liberar esta operação.' } },
            409,
          ),
      },
    ])
    renderWithProviders(<SecuritySettingsPage />)
    const dialog = await openDialog(user, /Solicitar mudança para Única/)
    const d = within(dialog)
    await user.click(d.getByRole('checkbox', { name: /Entendi as consequências/ }))
    await user.click(d.getByRole('button', { name: 'Continuar' }))
    await user.type(await d.findByLabelText(/Motivo da mudança/), 'Sócio deixou a operação financeira da empresa')
    await user.type(d.getByLabelText(/Código do autenticador/), '123456')
    await user.type(d.getByLabelText(/^Senha financeira/), 'financeira-forte-1')
    await user.click(d.getByRole('button', { name: 'Solicitar mudança' }))
    expect(await d.findByText(/Cadastre e ative o segundo Administrador Master para liberar esta operação/)).toBeInTheDocument()
    expect(d.getByRole('link', { name: 'Configurar agora' })).toHaveAttribute('href', '/configuracoes/seguranca')
  }, 60_000)

  it('convite pendente: e-mail mascarado, reenviar e revogar (com confirmação)', async () => {
    const user = userEvent.setup()
    signIn(makeMasterMe('DUAL', 'INVITE_PENDING'))
    const invite = { id: 'inv1', seat: 2, full_name: 'Maria Souza', email_masked: 'm***@rd.invalid', status: 'PENDING', expires_at: '2026-10-11T12:00:00Z', invited_by_name: 'Sócio A', sent_count: 1 }
    const r = base([
      { match: /\/security\/approval-mode$/, reply: () => info({ mode: 'DUAL', mode_version: 2, dual_approval_ready: false }) },
      { match: /\/security\/masters$/, reply: () => ({ approval_mode: 'DUAL', seats: MASTERS_DUAL_PENDING.seats, invite, can_invite: false }) },
      { method: 'POST', match: /\/security\/masters\/invites\/inv1\/resend$/, reply: () => ({ sent: true, expires_at: '2026-10-12T12:00:00Z' }) },
      { method: 'DELETE', match: /\/security\/masters\/invites\/inv1$/, reply: () => new Response(null, { status: 204 }) },
      { match: /\/me$/, reply: () => makeMasterMe('DUAL', 'NOT_INVITED') },
    ])
    renderWithProviders(<SecuritySettingsPage />)
    expect(await screen.findByText('Convite pendente')).toBeInTheDocument()
    expect(screen.getByText(/m\*\*\*@rd\.invalid/)).toBeInTheDocument()
    expect(screen.queryByLabelText(/E-mail do convidado/)).not.toBeInTheDocument() // só um convite por vez
    await user.click(screen.getByRole('button', { name: 'Reenviar convite' }))
    await waitFor(() => expect(r.find('POST', /resend$/)).toHaveLength(1))
    await user.click(screen.getByRole('button', { name: 'Revogar convite' }))
    const confirm = await screen.findByRole('alertdialog')
    await user.click(within(confirm).getByRole('button', { name: 'Revogar convite' }))
    await waitFor(() => expect(r.find('DELETE', /inv1$/)).toHaveLength(1))
    expect(document.body.innerHTML).not.toMatch(/token/i)
  }, 30_000)

  it('mudança em andamento: bloqueia novo pedido e leva ao pedido em Aprovações', async () => {
    signIn(makeMasterMe('DUAL', 'DONE'))
    base([
      {
        match: /\/security\/approval-mode$/,
        reply: () =>
          info({
            mode: 'DUAL',
            mode_version: 2,
            dual_approval_ready: true,
            pending_change_request: { id: 'req-mode', target_mode: 'SINGLE', requested_by_name: 'Sócio A', requested_at: '2026-10-08T13:00:00Z', expires_at: '2026-10-09T13:00:00Z' },
          }),
      },
      { match: /\/security\/masters$/, reply: () => MASTERS_DUAL_PENDING },
    ])
    renderWithProviders(<SecuritySettingsPage />)
    const pending = await screen.findByTestId('pending-mode-change')
    expect(pending).toHaveTextContent('Mudança para aprovação única em andamento')
    expect(pending).toHaveTextContent('Aguardando a aprovação do outro Administrador Master')
    expect(within(pending).getByRole('link', { name: 'Abrir o pedido' })).toHaveAttribute('href', '/financeiro/aprovacoes?pedido=req-mode')
    expect(screen.getByRole('button', { name: /Solicitar mudança para Única/ })).toBeDisabled()
  })
})

describe('Segurança da instância — versão vigente', () => {
  const h = (v: number, kind: string, from: 'SINGLE' | 'DUAL' | null, to: 'SINGLE' | 'DUAL', at: string) => ({
    mode_version: v, kind, from_mode: from, to_mode: to, source: v === 1 ? 'BOOTSTRAP_ENV' : 'APPROVAL_REQUEST',
    requested_by_name: v === 1 ? null : 'Sócio A', authorized_by_name: v === 1 ? null : 'Sócio A', reason: v === 1 ? null : 'Mudança de teste com motivo longo',
    approval_request_id: v === 1 ? null : 'r' + v, created_at: at,
  })

  it('versão 1 (inicial): mostra a data e a origem da inicialização', async () => {
    signIn(makeMasterMe('SINGLE'))
    base([{ match: /\/security\/approval-mode$/, reply: () => info() }])
    renderWithProviders(<SecuritySettingsPage />)
    const line = await screen.findByText(/^Versão 1/)
    expect(line).toHaveTextContent('Versão 1 · definida em 08/10/2026, 09:00 (primeira inicialização)')
  })

  it('versão 3 após transições: mostra data e tipo da VERSÃO VIGENTE (não a data da v1)', async () => {
    signIn(makeMasterMe('SINGLE'))
    base([
      {
        match: /\/security\/approval-mode$/,
        reply: () =>
          info({
            mode: 'SINGLE',
            mode_version: 3,
            history: [
              h(1, 'INITIALIZED', null, 'SINGLE', '2026-10-08T12:00:00Z'),
              h(2, 'SINGLE_TO_DUAL', 'SINGLE', 'DUAL', '2026-10-08T15:00:00Z'),
              h(3, 'DUAL_TO_SINGLE', 'DUAL', 'SINGLE', '2026-10-08T21:13:00Z'),
            ],
          }),
      },
    ])
    renderWithProviders(<SecuritySettingsPage />)
    const line = await screen.findByText(/^Versão 3/)
    expect(line).toHaveTextContent('em vigor desde 08/10/2026, 18:13 · Dupla → Única')
    expect(line).not.toHaveTextContent('primeira inicialização')
    expect(line).not.toHaveTextContent('09:00')
  })

  it('sem histórico visível (não Master) e versão > 1: não inventa data', async () => {
    signIn(makeMe({ permissions: ['settings.view', 'finance.view'] }))
    base([{ match: /\/security\/approval-mode$/, reply: () => info({ mode_version: 3, history: null, integrity: null, env: null }) }])
    renderWithProviders(<SecuritySettingsPage />)
    const line = await screen.findByText(/^Versão 3/)
    expect(line.textContent?.trim()).toBe('Versão 3')
  })
})

describe('Segurança da instância — alertas, permissões e estados', () => {
  it('divergência de ENV, variável não suportada e falha de integridade aparecem para o Master', async () => {
    signIn(makeMasterMe('SINGLE'))
    base([
      {
        match: /\/security\/approval-mode$/,
        reply: () => info({ env: { divergence: true, unsupported_var_present: true }, integrity: { ok: false } }),
      },
      { match: /\/security\/integrity$/, reply: () => ({ ok: false, problems: ['Selo do histórico de modo inválido (versão 1).'] }) },
    ])
    renderWithProviders(<SecuritySettingsPage />)
    expect(await screen.findByText('A configuração do ambiente difere do modo em vigor')).toBeInTheDocument()
    expect(screen.getByText(/só vale na primeira inicialização e foi ignorada/)).toBeInTheDocument()
    expect(screen.getByText('Variável de ambiente não utilizada')).toBeInTheDocument()
    expect(screen.getByText(/RD_DUAL_MASTER_APPROVAL não é lida pelo sistema/)).toBeInTheDocument()
    expect(await screen.findByText('Verificação de integridade da segurança falhou')).toBeInTheDocument()
    expect(await screen.findByText('Selo do histórico de modo inválido (versão 1).')).toBeInTheDocument()
  })

  it('quem não é Master vê o modo, sem ações de mudança e sem histórico', async () => {
    signIn(makeMe({ permissions: ['settings.view', 'finance.view'] }))
    base([{ match: /\/security\/approval-mode$/, reply: () => info({ history: null, integrity: null, env: null }) }])
    renderWithProviders(<SecuritySettingsPage />)
    expect(await screen.findByRole('group', { name: 'Modo atual: Única' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Solicitar mudança/ })).not.toBeInTheDocument()
    expect(screen.getByText(/Somente os Administradores Master solicitam a mudança do modo de aprovação/)).toBeInTheDocument()
    expect(screen.queryByText(/Histórico do modo/)).not.toBeInTheDocument()
  })

  it('carregando mostra esqueleto; 403 → sem permissão; 503 INSTANCE_NOT_INITIALIZED → erro com tentar novamente', async () => {
    signIn(makeMasterMe('SINGLE'))
    base([{ match: /\/security\/approval-mode$/, reply: () => jsonResponse({ error: { code: 'FORBIDDEN', message: 'Sem permissão.' } }, 403) }])
    renderWithProviders(<SecuritySettingsPage />)
    expect(screen.getByRole('status')).toHaveTextContent('Carregando a segurança da instância…')
    expect(await screen.findByText('Você não tem acesso a esta área')).toBeInTheDocument()
    expect(screen.getByText('settings.view')).toBeInTheDocument()

    vi.unstubAllGlobals()
    base([
      {
        match: /\/security\/approval-mode$/,
        reply: () => jsonResponse({ error: { code: 'INSTANCE_NOT_INITIALIZED', message: 'Instância não inicializada. Contate o responsável técnico.' } }, 503),
      },
    ])
    renderWithProviders(<SecuritySettingsPage />)
    expect(await screen.findByText('Instância não inicializada')).toBeInTheDocument()
    expect(screen.getByText('Instância não inicializada. Contate o responsável técnico.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Tentar novamente' })).toBeInTheDocument()
  })
})
