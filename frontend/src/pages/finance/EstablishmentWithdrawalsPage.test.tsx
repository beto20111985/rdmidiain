import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import EstablishmentWithdrawalsPage from './EstablishmentWithdrawalsPage'
import { useAuthStore } from '@/stores/authStore'
import { useContextStore } from '@/stores/contextStore'
import { makeMe, renderWithProviders } from '@/test/utils'
import { fetchRouter, page } from '@/components/finance/testing'

const EST_CTX = [{ key: 'est:org1', label: 'Minha unidade — Mercado Teste', kind: 'ESTABLISHMENT' as const, org_id: 'org1' }]

function signIn(permissions: string[]) {
  const me = makeMe({ permissions, contexts: EST_CTX })
  useAuthStore.setState({ status: 'authenticated', me, demo: false })
  useContextStore.getState().syncFromMe(EST_CTX)
}

function routes({ keyStatus = 'ACTIVE', ready = true }: { keyStatus?: string; ready?: boolean } = {}) {
  return fetchRouter([
    { match: /\/establishments\?/, reply: () => page([{ id: 'est1', org_id: 'org1', name: 'Mercado Teste', city: 'Goiânia', state: 'GO', timezone: 'America/Sao_Paulo' }]) },
    {
      match: /\/financial\/establishments\/est1\/balance/,
      reply: () => ({
        establishment_id: 'est1',
        pending_provisional_cents: 1050,
        pending_closed_cents: 2000,
        available_cents: 123456,
        in_transit_cents: 0,
        negative_balance_cents: 0,
        withdrawals_blocked: false,
        paid_cents: 99900,
        accumulated_cents: 300000,
      }),
    },
    {
      match: /\/financial\/establishments\/est1\/pix-keys/,
      reply: () =>
        page([
          {
            id: 'k1',
            establishment_id: 'est1',
            key_type: 'email',
            key_masked: 'do***@mercado.invalid',
            holder_name: 'Mercado Teste LTDA',
            holder_document_masked: '***456***90',
            active_from: '2026-10-07T15:00:00Z',
            created_at: '2026-10-07T03:00:00Z',
            revoked_at: null,
            status: keyStatus,
          },
        ]),
    },
    { match: /\/financial\/security\/status/, reply: () => ({ totp_enabled: ready, financial_password_set: ready, financial_password_changed_at: null }) },
    { match: /\/financial\/withdrawals\?/, reply: () => page([]) },
    // Contrato W-FIN pós-janela (d): 202 {sent, expires_in}
    { method: 'POST', match: /\/financial\/withdrawals\/email-code$/, reply: () => new Response(JSON.stringify({ sent: true, expires_in: 600 }), { status: 202, headers: { 'content-type': 'application/json' } }) },
    {
      method: 'POST',
      match: /\/financial\/withdrawals$/,
      reply: (c) => ({ id: 'w1', kind: 'ESTABLISHMENT', origin: 'MANUAL', establishment_id: 'est1', amount_cents: (c.body as { amount_cents: number }).amount_cents, status: 'REQUESTED', requested_at: '2026-10-07T20:00:00Z' }),
    },
  ])
}

beforeEach(() => useContextStore.getState().reset())
afterEach(() => vi.unstubAllGlobals())

describe('Minha unidade → Financeiro → Saques', () => {
  it('mostra o saldo em BRL a partir de centavos e a chave PIX somente leitura', async () => {
    signIn(['withdrawals.view', 'withdrawals.request', 'establishments.view'])
    routes()
    renderWithProviders(<EstablishmentWithdrawalsPage />)
    expect(await screen.findByText('R$ 1.234,56')).toBeInTheDocument() // disponível
    expect(screen.getByText('R$ 30,50')).toBeInTheDocument() // pendente = 2000 + 1050
    expect(screen.getByText('R$ 999,00')).toBeInTheDocument() // pago
    expect(await screen.findByText('do***@mercado.invalid')).toBeInTheDocument()
    expect(screen.getByText(/Somente leitura/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Trocar chave|Cadastrar chave/ })).not.toBeInTheDocument()
  })

  it('chave em período de segurança informa a data de liberação (12 h)', async () => {
    signIn(['withdrawals.view', 'withdrawals.request', 'establishments.view'])
    routes({ keyStatus: 'COOLDOWN' })
    renderWithProviders(<EstablishmentWithdrawalsPage />)
    expect(await screen.findByText('Liberada para saques em')).toBeInTheDocument()
    expect(screen.getByText(/os saques ficam bloqueados até 07\/10\/2026,? 12:00/)).toBeInTheDocument()
  })

  it('solicitar saque exige valor, TOTP, senha financeira e código por e-mail (do MESMO valor) e envia Idempotency-Key', async () => {
    const user = userEvent.setup()
    signIn(['withdrawals.view', 'withdrawals.request', 'establishments.view'])
    const r = routes()
    renderWithProviders(<EstablishmentWithdrawalsPage />)
    const submit = await screen.findByRole('button', { name: 'Solicitar saque' })
    await waitFor(() => expect(submit).not.toBeDisabled())
    await user.click(submit)
    expect(await screen.findByText('Informe o valor do saque.')).toBeInTheDocument()
    expect(screen.getByText('Informe o código de 6 dígitos do aplicativo autenticador.')).toBeInTheDocument()
    expect(screen.getByText('Informe sua senha financeira.')).toBeInTheDocument()
    expect(screen.getByText('Informe o código de 6 dígitos enviado por e-mail.')).toBeInTheDocument()
    // sem valor não dá para pedir o código (ele é vinculado ao valor)
    expect(screen.getByRole('button', { name: 'Enviar código por e-mail' })).toBeDisabled()
    expect(r.find('POST', /\/financial\/withdrawals$/)).toHaveLength(0)

    await user.type(screen.getByLabelText(/Valor do saque/), '1.000,50')
    await user.click(screen.getByRole('button', { name: 'Enviar código por e-mail' }))
    await waitFor(() => expect(r.find('POST', /\/withdrawals\/email-code$/)).toHaveLength(1))
    expect(r.find('POST', /\/withdrawals\/email-code$/)[0].body).toEqual({ establishment_id: 'est1', amount_cents: 100050 })
    expect(await screen.findByRole('button', { name: /Reenviar em (1:00|0:5\d)/ })).toBeDisabled()
    await user.type(screen.getByLabelText(/Código enviado por e-mail/), '445566')
    await user.type(screen.getByLabelText(/Código do autenticador/), '112233')
    await user.type(screen.getByLabelText(/^Senha financeira/), 'senha-financeira-teste')

    // mudar o valor depois do envio invalida o código (vale só para o valor enviado)
    await user.clear(screen.getByLabelText(/Valor do saque/))
    await user.type(screen.getByLabelText(/Valor do saque/), '999,00')
    await user.click(submit)
    expect(await screen.findByText(/O valor mudou depois do envio do código/)).toBeInTheDocument()
    expect(r.find('POST', /\/financial\/withdrawals$/)).toHaveLength(0)
    expect(screen.getByLabelText(/^Senha financeira/)).toHaveValue('senha-financeira-teste') // resto preservado

    await user.clear(screen.getByLabelText(/Valor do saque/))
    await user.type(screen.getByLabelText(/Valor do saque/), '1.000,50')
    await user.click(screen.getByRole('button', { name: 'Enviar código por e-mail' }))
    await waitFor(() => expect(r.find('POST', /\/withdrawals\/email-code$/)).toHaveLength(2))
    await user.type(screen.getByLabelText(/Código enviado por e-mail/), '778899')
    await user.click(submit)
    await waitFor(() => expect(r.find('POST', /\/financial\/withdrawals$/)).toHaveLength(1))
    const call = r.find('POST', /\/financial\/withdrawals$/)[0]
    expect(call.body).toEqual({ establishment_id: 'est1', amount_cents: 100050, totp_code: '112233', financial_password: 'senha-financeira-teste', email_code: '778899' })
    expect(call.headers['Idempotency-Key']).toMatch(/^[0-9a-f-]{36}$/)
  }, 20_000)

  it('sem 2FA/senha financeira o formulário fica desabilitado com orientação', async () => {
    signIn(['withdrawals.view', 'withdrawals.request', 'establishments.view'])
    routes({ ready: false })
    renderWithProviders(<EstablishmentWithdrawalsPage />)
    expect(await screen.findByText('Ative a verificação em duas etapas')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Solicitar saque' })).toBeDisabled()
  })

  it('perfil sem withdrawals.request só visualiza (sem formulário de saque)', async () => {
    signIn(['withdrawals.view', 'establishments.view'])
    routes()
    renderWithProviders(<EstablishmentWithdrawalsPage />)
    expect(await screen.findByText('Seu perfil só visualiza os saques')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Solicitar saque' })).not.toBeInTheDocument()
  })
})
