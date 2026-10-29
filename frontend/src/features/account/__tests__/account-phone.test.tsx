/**
 * Minha conta — edição do telefone do próprio usuário (PATCH /me {phone}) e retorno ao checkout
 * (PAYER_DATA_INCOMPLETE missing=[phone] → "Completar meus dados" → salvar → volta ao carrinho).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Route, Routes, useLocation } from 'react-router-dom'
import AccountPage from '../pages/AccountPage'
import { jsonResponse, makeMe, mockFetch, renderWithProviders } from '@/test/utils'
import { useAuthStore } from '@/stores/authStore'
import { sentBodies } from '@/pages/campaigns/__tests__/fixtures'

const toastSuccess = vi.hoisted(() => vi.fn())
vi.mock('sonner', () => ({ toast: { success: toastSuccess, error: vi.fn() }, Toaster: () => null }))

function signIn(phone: string | null) {
  const me = makeMe()
  useAuthStore.setState({ status: 'authenticated', me: { ...me, user: { ...me.user, phone } }, demo: false })
}

function Where() {
  const loc = useLocation()
  return <p data-testid="where">{`${loc.pathname}${loc.search}`}</p>
}

function renderAccount(route = '/minha-conta') {
  return renderWithProviders(
    <Routes>
      <Route path="/minha-conta" element={<AccountPage />} />
      <Route path="/anunciante/carrinho" element={<Where />} />
    </Routes>,
    { route },
  )
}

function patchCalls(fetchMock: ReturnType<typeof mockFetch>) {
  return fetchMock.mock.calls.filter(([u, init]) => String(u).endsWith('/me') && (init as RequestInit).method === 'PATCH')
}

beforeEach(() => toastSuccess.mockReset())

describe('Minha conta — telefone', () => {
  it('adiciona o telefone com máscara BR, envia só os dígitos e mostra o salvo', async () => {
    signIn(null)
    const fetchMock = mockFetch((url, init) => {
      if (url.endsWith('/me') && init.method === 'PATCH') return jsonResponse({ user: { id: 'u1', phone: '5511900000000' } })
      return jsonResponse({})
    })
    const user = userEvent.setup()
    renderAccount()
    expect(screen.getByTestId('account-phone')).toHaveTextContent('—')
    await user.click(screen.getByRole('button', { name: 'Adicionar telefone' }))
    const input = screen.getByRole('textbox', { name: 'Telefone (com DDD)' })
    await user.type(input, '11900000000')
    expect(input).toHaveValue('(11) 90000-0000')
    await user.click(screen.getByRole('button', { name: 'Salvar telefone' }))
    await waitFor(() => expect(screen.getByTestId('account-phone')).toHaveTextContent('(11) 90000-0000'))
    expect(sentBodies({ mock: { calls: patchCalls(fetchMock) } })).toEqual([{ phone: '11900000000' }])
    expect(useAuthStore.getState().me?.user.phone).toBe('5511900000000')
    expect(toastSuccess).toHaveBeenCalledWith('Telefone salvo.')
    expect(screen.queryByRole('textbox', { name: 'Telefone (com DDD)' })).not.toBeInTheDocument()
  })

  it('apagar a máscara apaga o dígito; fixo de 10 dígitos formata (DD) NNNN-NNNN', async () => {
    signIn('5511912345678')
    mockFetch(() => jsonResponse({}))
    const user = userEvent.setup()
    renderAccount()
    expect(screen.getByTestId('account-phone')).toHaveTextContent('(11) 91234-5678')
    await user.click(screen.getByRole('button', { name: 'Alterar telefone' }))
    const input = screen.getByRole('textbox', { name: 'Telefone (com DDD)' })
    expect(input).toHaveValue('(11) 91234-5678')
    await user.clear(input)
    await user.type(input, '1133334444')
    expect(input).toHaveValue('(11) 3333-4444')
    // apagar o hífen (caractere da máscara) remove o último dígito em vez de a máscara recolocá-lo
    fireEvent.change(input, { target: { value: '(11) 33334444' } })
    expect(input).toHaveValue('(11) 3333-444')
  })

  it.each([
    ['1190000', 'Use (DD) NNNN-NNNN para fixo ou (DD) 9NNNN-NNNN para celular.'],
    ['01912345678', 'DDD inválido.'],
    ['11812345678', 'Celular com DDD tem 11 dígitos e começa com 9 depois do DDD.'],
    ['1193334444', 'Telefone fixo começa com 2, 3, 4 ou 5 depois do DDD.'],
  ])('inválido (%s): mostra o erro e não envia', async (typed, message) => {
    signIn(null)
    const fetchMock = mockFetch(() => jsonResponse({}))
    const user = userEvent.setup()
    renderAccount()
    await user.click(screen.getByRole('button', { name: 'Adicionar telefone' }))
    await user.type(screen.getByRole('textbox', { name: 'Telefone (com DDD)' }), typed)
    await user.click(screen.getByRole('button', { name: 'Salvar telefone' }))
    expect(await screen.findByText(message)).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Telefone (com DDD)' })).toHaveAttribute('aria-invalid', 'true')
    expect(patchCalls(fetchMock)).toHaveLength(0)
  })

  it('erro do servidor (409 PHONE_IN_USE): mensagem do envelope, editor aberto e valor mantido', async () => {
    signIn(null)
    mockFetch((url, init) => {
      if (url.endsWith('/me') && init.method === 'PATCH')
        return jsonResponse({ error: { code: 'PHONE_IN_USE', message: 'Telefone já cadastrado em outra conta.', details: {} } }, 409)
      return jsonResponse({})
    })
    const user = userEvent.setup()
    renderAccount()
    await user.click(screen.getByRole('button', { name: 'Adicionar telefone' }))
    await user.type(screen.getByRole('textbox', { name: 'Telefone (com DDD)' }), '11900000000')
    await user.click(screen.getByRole('button', { name: 'Salvar telefone' }))
    expect(await screen.findByText('Telefone já cadastrado em outra conta.')).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Telefone (com DDD)' })).toHaveValue('(11) 90000-0000')
    expect(useAuthStore.getState().me?.user.phone).toBeNull()
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('vindo do checkout (?voltar=): abre o editor, avisa e volta ao carrinho com seleção e cupom após salvar', async () => {
    signIn(null)
    mockFetch((url, init) => {
      if (url.endsWith('/me') && init.method === 'PATCH') return jsonResponse({ user: { id: 'u1', phone: '5511900000000' } })
      return jsonResponse({})
    })
    const user = userEvent.setup()
    const back = '/anunciante/carrinho?c=c1&cupom=DEMO10'
    renderAccount(`/minha-conta?voltar=${encodeURIComponent(back)}`)
    expect(screen.getByTestId('account-return-notice')).toHaveTextContent('Depois de salvar, você volta automaticamente ao pagamento')
    expect(screen.getByRole('link', { name: 'Voltar ao pagamento sem salvar' })).toHaveAttribute('href', back)
    const input = screen.getByRole('textbox', { name: 'Telefone (com DDD)' })
    await user.type(input, '11900000000')
    await user.click(screen.getByRole('button', { name: 'Salvar telefone' }))
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent(back))
  })

  it('ignora `voltar` externo (sem redirecionamento aberto)', () => {
    signIn(null)
    mockFetch(() => jsonResponse({}))
    renderAccount(`/minha-conta?voltar=${encodeURIComponent('//exemplo.invalid/x')}`)
    expect(screen.queryByTestId('account-return-notice')).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: 'Telefone (com DDD)' })).not.toBeInTheDocument()
  })
})
