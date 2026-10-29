import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ProposalForm } from './proposal-form'
import { useAuthStore } from '@/stores/authStore'
import { useContextStore } from '@/stores/contextStore'
import { jsonResponse, makeMe, renderWithProviders } from '@/test/utils'
import { fetchRouter, page } from '@/components/finance/testing'
import { INSTANCE_CLIENT_MESSAGE, clientOptionToPicker } from './client-options'

function signIn(permissions: string[]) {
  const me = makeMe({ permissions, user: { id: 'u-vendas', name: 'Vendas', email: 'v@rd.invalid', mfa_enabled: false, is_admin_master: false, theme: null } })
  useAuthStore.setState({ status: 'authenticated', me, demo: false })
  useContextStore.getState().syncFromMe(me.contexts)
}

const OPTIONS = [
  { id: 'org1', name: 'Alfa Comércio Ltda', trade_name: 'Alfa', city: 'Goiânia', uf: 'GO', business_roles: ['ADVERTISER'] },
  { id: 'org2', name: 'Beta Comunicação', trade_name: null, city: null, uf: null, business_roles: ['AGENCY'] },
]

beforeEach(() => useContextStore.getState().reset())
afterEach(() => vi.unstubAllGlobals())

describe('Proposta: cliente pelo client-options (W-BIZ)', () => {
  it('rótulo do cliente traz razão social, cidade/UF e papel de agência', () => {
    expect(clientOptionToPicker(OPTIONS[0])).toEqual({ id: 'org1', label: 'Alfa', description: 'Alfa Comércio Ltda · Goiânia/GO' })
    expect(clientOptionToPicker(OPTIONS[1])).toEqual({ id: 'org2', label: 'Beta Comunicação', description: 'Agência' })
  })

  it('busca clientes elegíveis no client-options e mapeia CLIENT_IS_INSTANCE no campo do cliente', async () => {
    const user = userEvent.setup()
    signIn(['crm.view', 'crm.manage'])
    const r = fetchRouter([
      { match: /\/crm\/leads/, reply: () => page([]) },
      { match: /\/crm\/client-options/, reply: () => ({ items: OPTIONS }) },
      {
        method: 'POST',
        match: /\/crm\/proposals$/,
        reply: () => jsonResponse({ error: { code: 'CLIENT_IS_INSTANCE', message: 'Organização da instância.' } }, 400),
      },
    ])
    renderWithProviders(<ProposalForm onSaved={vi.fn()} />)

    // A lista vem do client-options (nunca de /organizations).
    const alfa = await screen.findByRole('button', { name: /Alfa · Alfa Comércio Ltda · Goiânia\/GO/ })
    expect(r.find('GET', /\/crm\/client-options\?.*limit=20/).length).toBeGreaterThan(0)
    expect(r.find('GET', /\/organizations/)).toHaveLength(0)

    await user.type(screen.getByLabelText(/Título/), 'Proposta de teste')
    await user.type(screen.getByLabelText(/Descrição/), 'Pacote 10 telas')
    await user.type(screen.getByLabelText(/Valor unitário/), '1000,00')
    await user.click(alfa)
    expect(screen.getByRole('button', { name: 'Trocar organização (cliente)' })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Criar rascunho' }))
    await waitFor(() => expect(r.find('POST', /\/crm\/proposals$/)).toHaveLength(1))
    expect((r.find('POST', /\/crm\/proposals$/)[0].body as { org_id: string }).org_id).toBe('org1')
    expect(await screen.findByText(INSTANCE_CLIENT_MESSAGE)).toBeInTheDocument()
  }, 20_000)
})
