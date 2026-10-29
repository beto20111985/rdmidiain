/**
 * AÇÃO 004 (decisão do proprietário) — empresa ANUNCIANTE: telefone com DDD (e e-mail) obrigatórios no cadastro,
 * os mesmos dados que o pagamento exige do pagador (PAYER_DATA_INCOMPLETE). Parceiro/agência: sem mudança.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes } from 'react-router-dom'
import OrganizationNewPage from '../organizations/OrganizationNewPage'
import { OrganizationForm } from '../organizations/OrganizationForm'
import type { Organization } from '../types'
import { renderWithProviders } from '@/test/utils'
import { jsonResponse, mockApi, resetSession, signInAs, withPageMeta } from '@/test/adminTestUtils'

vi.setConfig({ testTimeout: 20_000 })

beforeEach(() => resetSession())
afterEach(() => vi.unstubAllGlobals())

function renderNew() {
  signInAs({ permissions: ['organizations.view', 'organizations.manage'] })
  return renderWithProviders(
    withPageMeta(
      <Routes>
        <Route path="/empresas/nova" element={<OrganizationNewPage />} />
        <Route path="/empresas/:id" element={<p>detalhe da empresa</p>} />
      </Routes>,
    ),
    { route: '/empresas/nova' },
  )
}

describe('empresa anunciante: telefone obrigatório (dado do pagador)', () => {
  it('não envia sem telefone/e-mail e mostra o erro em cada campo; com os dados, envia o telefone mascarado', async () => {
    const user = userEvent.setup()
    const { calls } = mockApi((c) =>
      c.method === 'POST' && c.path === '/organizations' ? jsonResponse({ id: 'o9', name: 'Britas Teste', trade_name: null }, 201) : undefined,
    )
    renderNew()
    await user.type(screen.getByLabelText(/^CNPJ/), '11.222.333/0001-81')
    await user.type(screen.getByLabelText(/Razão social/), 'Britas Teste')
    await user.click(screen.getByRole('checkbox', { name: 'Anunciante' }))
    await user.click(screen.getByRole('button', { name: 'Cadastrar empresa' }))
    expect(await screen.findByText('Telefone com DDD obrigatório para anunciante (exigido no pagamento).')).toBeInTheDocument()
    expect(screen.getByText('E-mail obrigatório para anunciante (exigido no pagamento).')).toBeInTheDocument()
    expect(calls.some((c) => c.method === 'POST')).toBe(false)

    // sem DDD (8 dígitos) também não serve para o pagamento
    await user.type(screen.getByLabelText(/^Telefone/), '99990000')
    await user.type(screen.getByLabelText(/^E-mail/), 'financeiro@britas.com.br')
    await user.click(screen.getByRole('button', { name: 'Cadastrar empresa' }))
    expect(await screen.findByText(/Use \(DD\) NNNN-NNNN para fixo/)).toBeInTheDocument()
    expect(calls.some((c) => c.method === 'POST')).toBe(false)

    await user.clear(screen.getByLabelText(/^Telefone/))
    await user.type(screen.getByLabelText(/^Telefone/), '61999990000')
    expect(screen.getByLabelText(/^Telefone/)).toHaveValue('(61) 99999-0000') // mesma máscara de "Minha conta"
    await user.click(screen.getByRole('button', { name: 'Cadastrar empresa' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true))
    expect(calls.find((c) => c.method === 'POST')!.body).toMatchObject({
      business_roles: ['ADVERTISER'],
      phone: '(61) 99999-0000',
      email: 'financeiro@britas.com.br',
    })
  })

  it('parceiro (não paga campanha): telefone e e-mail continuam opcionais', async () => {
    const user = userEvent.setup()
    const { calls } = mockApi((c) =>
      c.method === 'POST' && c.path === '/organizations' ? jsonResponse({ id: 'o8', name: 'Bar Parceiro', trade_name: null }, 201) : undefined,
    )
    renderNew()
    await user.type(screen.getByLabelText(/^CNPJ/), '11.222.333/0001-81')
    await user.type(screen.getByLabelText(/Razão social/), 'Bar Parceiro')
    await user.click(screen.getByRole('checkbox', { name: 'Parceiro (estabelecimento)' }))
    await user.click(screen.getByRole('button', { name: 'Cadastrar empresa' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true))
  })

  it('erro do servidor (ADVERTISER_PAYER_DATA_REQUIRED) aparece junto ao campo Telefone', async () => {
    const user = userEvent.setup()
    mockApi((c) =>
      c.method === 'POST' && c.path === '/organizations'
        ? jsonResponse(
            {
              error: {
                code: 'ADVERTISER_PAYER_DATA_REQUIRED',
                message: 'Empresa anunciante precisa de e-mail e telefone com DDD (dados exigidos no pagamento).',
                details: { missing: ['phone'], errors: [{ loc: ['body', 'phone'], msg: 'Telefone recusado pelo servidor.' }] },
              },
            },
            422,
          )
        : undefined,
    )
    renderNew()
    await user.type(screen.getByLabelText(/^CNPJ/), '11.222.333/0001-81')
    await user.type(screen.getByLabelText(/Razão social/), 'Britas Teste')
    await user.click(screen.getByRole('checkbox', { name: 'Anunciante' }))
    await user.type(screen.getByLabelText(/^E-mail/), 'financeiro@britas.com.br')
    await user.type(screen.getByLabelText(/^Telefone/), '61999990000')
    await user.click(screen.getByRole('button', { name: 'Cadastrar empresa' }))
    const msg = await screen.findByText('Telefone recusado pelo servidor.')
    expect(msg.closest('[data-slot="field"]')).toHaveTextContent('Telefone')
  })

  it('edição de anunciante antigo sem telefone/e-mail: o formulário pede os dois (contato do pagador) antes de salvar', async () => {
    const user = userEvent.setup()
    const onSubmit = vi.fn().mockResolvedValue({})
    const legacy = {
      id: 'o1', name: 'BRITAS TESTE', trade_name: null, document: '11222333000181', document_type: 'CNPJ',
      business_roles: ['ADVERTISER'], email: null, phone: null, city: 'Brasília', state: 'DF',
    } as unknown as Organization
    renderWithProviders(<OrganizationForm initial={legacy} submitLabel="Salvar alterações" onSubmit={onSubmit} />)
    await user.type(screen.getByLabelText(/Nome fantasia/), 'Britas')
    await user.click(screen.getByRole('button', { name: 'Salvar alterações' }))
    expect(await screen.findByText('Telefone com DDD obrigatório para anunciante (exigido no pagamento).')).toBeInTheDocument()
    expect(screen.getByText('E-mail obrigatório para anunciante (exigido no pagamento).')).toBeInTheDocument()
    expect(onSubmit).not.toHaveBeenCalled()
    await user.type(screen.getByLabelText(/^Telefone/), '6133334444')
    await user.type(screen.getByLabelText(/^E-mail/), 'contato@britas.com.br')
    await user.click(screen.getByRole('button', { name: 'Salvar alterações' }))
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    expect(onSubmit.mock.calls[0][0]).toMatchObject({ phone: '(61) 3333-4444', email: 'contato@britas.com.br', trade_name: 'Britas' })
  })
})
