import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ApprovalsPage from './ApprovalsPage'
import { useAuthStore } from '@/stores/authStore'
import { useContextStore } from '@/stores/contextStore'
import { makeMe, renderWithProviders } from '@/test/utils'
import { fetchRouter, page } from '@/components/finance/testing'
import type { ApprovalRequest } from './api'

const ME = 'aaaaaaaa-0000-4000-8000-000000000001'
const OTHER = 'bbbbbbbb-0000-4000-8000-000000000002'

function req(partial: Partial<ApprovalRequest>): ApprovalRequest {
  return {
    id: 'r1',
    action_type: 'EXPENSE_CREATE',
    action_label: 'despesa manual',
    payload: { description: 'Internet', amount_cents: 15000, category: 'internet', competence_date: '2026-10-01' },
    payload_sha256: 'a'.repeat(64),
    amount_cents: 15000,
    reason: 'Conta de internet do escritório',
    requested_by: OTHER,
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
    requires_email_code: false,
    can_approve: true,
    ...partial,
  }
}

const MINE = req({ id: 'mine', requested_by: ME, can_approve: false, action_label: 'retirada (PIX OUT) da RD', action_type: 'RD_WITHDRAWAL', requires_email_code: true })
const THEIRS = req({ id: 'theirs' })
const PIX = req({ id: 'pix', action_type: 'RD_WITHDRAWAL', action_label: 'retirada (PIX OUT) da RD', requires_email_code: true, payload: { amount_cents: 500000, pix_key_type: 'email', pix_key_masked: 'so***@rd.invalid', purpose: 'retirada' }, amount_cents: 500000 })

function signIn({ master = true, permissions = ['finance.approve', 'finance.view', 'users.view'] } = {}) {
  const me = makeMe({ permissions, user: { id: ME, name: 'Sócio A', email: 'a@rd.invalid', mfa_enabled: true, is_admin_master: master, theme: null } })
  useAuthStore.setState({ status: 'authenticated', me, demo: false })
  useContextStore.getState().syncFromMe(me.contexts)
}

function routes(list: ApprovalRequest[]) {
  return fetchRouter([
    { match: /\/financial\/security\/status/, reply: () => ({ totp_enabled: true, financial_password_set: true, financial_password_changed_at: null }) },
    { match: /\/users\?/, reply: () => page([{ id: OTHER, name: 'Sócio B' }]) },
    { match: /\/financial\/approvals\/[a-z]+\/events/, reply: () => ({ items: [] }) },
    { match: /\/financial\/approvals\/(mine|theirs|pix)$/, reply: (c) => list.find((r) => c.url.endsWith(`/${r.id}`)) },
    { method: 'POST', match: /\/financial\/approvals\/[a-z]+\/approve/, reply: (c) => ({ ...list.find((r) => c.url.includes(`/${r.id}/`)), status: 'APPROVED', execution_state: 'EXECUTED' }) },
    { method: 'POST', match: /\/financial\/approvals\/[a-z]+\/email-code/, reply: () => ({ sent: true, expires_in: 600 }) },
    { match: /\/financial\/approvals\?/, reply: () => page(list) },
  ])
}

beforeEach(() => {
  useContextStore.getState().reset()
})
afterEach(() => vi.unstubAllGlobals())

describe('Aprovações duplas', () => {
  it('quem não é Admin Master vê "sem permissão" e nenhuma lista', async () => {
    signIn({ master: false, permissions: ['finance.approve', 'finance.view'] })
    const r = routes([THEIRS])
    renderWithProviders(<ApprovalsPage />)
    expect(screen.getByText('Exclusivo dos Admin Master')).toBeInTheDocument()
    expect(r.find('GET', /\/financial\/approvals\?/)).toHaveLength(0)
  })

  it('o solicitante NÃO vê botão de aprovar o próprio pedido (só cancelar)', async () => {
    const user = userEvent.setup()
    signIn()
    routes([MINE, THEIRS])
    renderWithProviders(<ApprovalsPage />)
    // Lista: o pedido do outro sócio pede decisão; o meu só tem "Ver detalhes"
    expect(await screen.findByRole('button', { name: /Revisar pedido: despesa manual/ })).toHaveTextContent('Revisar e decidir')
    const mineBtn = screen.getByRole('button', { name: /Revisar pedido: retirada \(PIX OUT\) da RD/ })
    expect(mineBtn).toHaveTextContent('Ver detalhes')
    expect(await screen.findByText(/1 pedido aguarda a sua aprovação/)).toBeInTheDocument()

    await user.click(mineBtn)
    const sheet = await screen.findByRole('dialog')
    expect(await within(sheet).findByText('Você é o solicitante')).toBeInTheDocument()
    expect(within(sheet).queryByRole('button', { name: 'Aprovar pedido' })).not.toBeInTheDocument()
    expect(within(sheet).queryByRole('form', { name: 'Aprovar pedido' })).not.toBeInTheDocument()
    expect(within(sheet).getByRole('button', { name: 'Cancelar solicitação' })).toBeInTheDocument()
  })

  it('aprovar exige TOTP + senha financeira + código por e-mail (nada é enviado sem eles)', async () => {
    const user = userEvent.setup()
    signIn()
    const r = routes([THEIRS])
    renderWithProviders(<ApprovalsPage />)
    await user.click(await screen.findByRole('button', { name: /Revisar pedido: despesa manual/ }))
    const sheet = await screen.findByRole('dialog')
    const approve = await within(sheet).findByRole('button', { name: 'Aprovar pedido' })
    await user.click(approve)
    expect(await within(sheet).findByText('Informe o código de 6 dígitos do aplicativo autenticador.')).toBeInTheDocument()
    expect(within(sheet).getByText('Informe sua senha financeira.')).toBeInTheDocument()
    expect(within(sheet).getByText('Informe o código de 6 dígitos enviado por e-mail.')).toBeInTheDocument()
    expect(within(sheet).getByLabelText(/Código do autenticador/)).toHaveAttribute('aria-invalid', 'true')
    expect(r.find('POST', /\/approve/)).toHaveLength(0)
    // conteúdo revisado é exibido (valor em BRL a partir de centavos)
    expect(within(sheet).getAllByText('R$ 150,00').length).toBeGreaterThan(0)
  }, 20_000)

  it('TODA aprovação (mesmo sem PIX OUT) exige o código por e-mail; reenvio com espera visível; envia o SHA-256 revisado', async () => {
    const user = userEvent.setup()
    signIn()
    const r = routes([THEIRS])
    renderWithProviders(<ApprovalsPage />)
    await user.click(await screen.findByRole('button', { name: /Revisar pedido: despesa manual/ }))
    const sheet = await screen.findByRole('dialog')
    await user.type(await within(sheet).findByLabelText(/Código do autenticador/), '123456')
    await user.type(within(sheet).getByLabelText(/^Senha financeira/), 'segredo-de-teste')
    await user.click(within(sheet).getByRole('button', { name: 'Aprovar pedido' }))
    expect(await within(sheet).findByText('Informe o código de 6 dígitos enviado por e-mail.')).toBeInTheDocument()
    expect(r.find('POST', /\/approve/)).toHaveLength(0)

    await user.click(within(sheet).getByRole('button', { name: 'Enviar código por e-mail' }))
    await waitFor(() => expect(r.find('POST', /\/email-code/)).toHaveLength(1))
    expect(r.find('POST', /\/email-code/)[0].url).toMatch(/\/financial\/approvals\/theirs\/email-code$/)
    expect(r.find('POST', /\/email-code/)[0].body).toBeUndefined() // sem corpo (contrato)
    expect(await within(sheet).findByRole('button', { name: /Reenviar em (1:00|0:5\d)/ })).toBeDisabled()
    expect(within(sheet).getByText(/vale até \d{2}:\d{2} \(10 min\) e uma única vez/)).toBeInTheDocument()
    await user.type(within(sheet).getByLabelText(/Código enviado por e-mail/), '654321')
    await user.clear(within(sheet).getByLabelText(/Código do autenticador/))
    await user.type(within(sheet).getByLabelText(/Código do autenticador/), '123456')
    await user.click(within(sheet).getByRole('button', { name: 'Aprovar pedido' }))
    await waitFor(() => expect(r.find('POST', /\/approve/)).toHaveLength(1))
    const call = r.find('POST', /\/approve/)[0]
    expect(call.body).toEqual({ totp_code: '123456', financial_password: 'segredo-de-teste', payload_sha256: 'a'.repeat(64), email_code: '654321' })
    expect(call.headers.Authorization).toBeUndefined() // token só existe em memória após login (aqui não há sessão real)
  }, 20_000)
  it('código errado/expirado/reutilizado: mensagem clara, código invalidado, resto do formulário preservado; envio limitado (429) mostra espera', async () => {
    const user = userEvent.setup()
    signIn()
    let sends = 0
    const r = fetchRouter([
      { match: /\/financial\/security\/status/, reply: () => ({ totp_enabled: true, financial_password_set: true, financial_password_changed_at: null }) },
      { match: /\/users\?/, reply: () => page([]) },
      { match: /\/financial\/approvals\/pix\/events/, reply: () => ({ items: [] }) },
      { match: /\/financial\/approvals\/pix$/, reply: () => ({ ...PIX, requested_by_name: 'Sócio B' }) },
      {
        method: 'POST',
        match: /\/financial\/approvals\/pix\/email-code/,
        reply: () => {
          sends += 1
          return sends === 1
            ? { sent: true, expires_in: 600 }
            : new Response(JSON.stringify({ error: { code: 'FINANCIAL_EMAIL_CODE_RATE_LIMITED', message: 'Muitos envios.', details: { retry_after_s: 120 } } }), { status: 429, headers: { 'content-type': 'application/json' } })
        },
      },
      {
        method: 'POST',
        match: /\/financial\/approvals\/pix\/approve/,
        reply: () => new Response(JSON.stringify({ error: { code: 'FINANCIAL_EMAIL_CODE_INVALID', message: 'Código inválido.' } }), { status: 403, headers: { 'content-type': 'application/json' } }),
      },
      { match: /\/financial\/approvals\?/, reply: () => page([{ ...PIX, requested_by_name: 'Sócio B' }]) },
    ])
    renderWithProviders(<ApprovalsPage />)
    expect(await screen.findByText('Sócio B')).toBeInTheDocument() // nome resolvido pelo servidor
    await user.click(screen.getByRole('button', { name: /Revisar pedido: retirada/ }))
    const sheet = await screen.findByRole('dialog')
    await user.click(await within(sheet).findByRole('button', { name: 'Enviar código por e-mail' }))
    await waitFor(() => expect(sends).toBe(1))
    await user.type(within(sheet).getByLabelText(/Código do autenticador/), '123456')
    await user.type(within(sheet).getByLabelText(/^Senha financeira/), 'segredo-de-teste')
    await user.type(within(sheet).getByLabelText(/Código enviado por e-mail/), '111111')
    await user.click(within(sheet).getByRole('button', { name: 'Aprovar pedido' }))
    expect(await within(sheet).findByText(/Código inválido, expirado, já usado ou de outra operação\. Peça um novo código\./)).toBeInTheDocument()
    expect(within(sheet).getByLabelText(/Código enviado por e-mail/)).toHaveValue('')
    expect(within(sheet).getByLabelText(/Código do autenticador/)).toHaveValue('') // TOTP é de uso único
    expect(within(sheet).getByLabelText(/^Senha financeira/)).toHaveValue('segredo-de-teste') // preservada
    // o código invalidado libera novo envio imediatamente; o servidor limita (429) → espera visível
    await user.click(within(sheet).getByRole('button', { name: 'Enviar código por e-mail' }))
    expect(await within(sheet).findByText(/Muitos envios de código em pouco tempo/)).toBeInTheDocument()
    expect(within(sheet).getByRole('button', { name: /Reenviar em (2:00|1:5\d)/ })).toBeDisabled()
    expect(r.find('POST', /\/approve/)).toHaveLength(1)
  }, 20_000)
})
