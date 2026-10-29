/**
 * Aviso do 2º Master (SPEC AÇÃO 003 §11.3–§11.5): modal e banner SOMENTE na aprovação DUPLA, para o
 * titular de assento e com o 2º Master pendente; NUNCA na ÚNICA. Convite pelo formulário e sincronização
 * em tempo real (`security.status_changed` → recarrega /me).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SecondMasterBanner } from './SecondMasterBanner'
import { SecondMasterModal } from './SecondMasterModal'
import { SecurityStatusSync } from './SecurityStatusSync'
import { InviteSecondMasterForm } from './InviteSecondMasterForm'
import { useSecurityUiStore } from './uiStore'
import { useAuthStore } from '@/stores/authStore'
import { tokenStore } from '@/lib/http'
import { RealtimeContext, type RealtimeHandler } from '@/lib/realtime/context'
import { invalidationsFor } from '@/lib/realtime/eventMap'
import { fetchRouter } from '@/components/finance/testing'
import { makeMasterMe } from '@/test/securityFixtures'
import { jsonResponse, makeMe } from '@/test/utils'
import { TooltipProvider } from '@/components/ui/tooltip'
import type { Me } from '@/features/auth/types'

const INVITE = { id: 'i1', seat: 2, full_name: 'Maria Souza', email_masked: 'm***@rd.invalid', status: 'PENDING', expires_at: '2026-10-11T12:00:00Z', invited_by_name: 'Sócio A', sent_count: 1 }

function mount(me: Me, route = '/visao-geral') {
  useAuthStore.setState({ status: 'authenticated', me, demo: false })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={[route]}>
          <SecondMasterBanner />
          <SecondMasterModal />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  tokenStore.clear()
  useSecurityUiStore.getState().reset()
})
afterEach(() => vi.unstubAllGlobals())

describe('modal e banner do 2º Master', () => {
  it('DUPLA + Master + não convidado: modal na sessão com as ações e banner "Configurar agora"', async () => {
    const user = userEvent.setup()
    mount(makeMasterMe('DUAL', 'NOT_INVITED'))
    const dialog = await screen.findByRole('dialog', { name: 'Configure o segundo Administrador Master' })
    expect(dialog).toHaveTextContent(
      'Esta instalação utiliza dupla aprovação financeira. Para liberar operações financeiras sensíveis, é necessário cadastrar um segundo Administrador Master independente.',
    )
    expect(screen.getByRole('button', { name: 'Convidar agora' })).toBeInTheDocument()
    // banner persistente
    const banner = screen.getByTestId('second-master-banner')
    expect(banner).toHaveTextContent('Configuração de segurança pendente')
    expect(banner).toHaveTextContent('Cadastre o segundo Administrador Master para liberar operações financeiras que exigem dupla aprovação.')
    // (com o modal aberto o resto da página fica oculto para tecnologias assistivas: hidden)
    expect(screen.getByRole('link', { name: 'Configurar agora', hidden: true })).toHaveAttribute('href', '/configuracoes/seguranca')

    await user.click(screen.getByRole('button', { name: 'Fazer depois' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.getByTestId('second-master-banner')).toBeInTheDocument() // o banner continua
    expect(useSecurityUiStore.getState().secondMasterModalDismissed).toBe(true)
  })

  it('o modal volta a cada sessão (estado zerado no logout/login)', async () => {
    useSecurityUiStore.getState().dismissSecondMasterModal()
    mount(makeMasterMe('DUAL', 'NOT_INVITED'))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    useSecurityUiStore.getState().reset() // nova sessão
    expect(await screen.findByRole('dialog', { name: 'Configure o segundo Administrador Master' })).toBeInTheDocument()
  })

  it('"Convidar agora" abre o formulário no próprio modal; envia nome, e-mail e TOTP; nunca recebe link/token', async () => {
    const user = userEvent.setup()
    const r = fetchRouter([
      { method: 'POST', match: /\/security\/masters\/invites$/, reply: () => INVITE },
      { match: /\/me$/, reply: () => makeMasterMe('DUAL', 'INVITE_PENDING') },
      { match: /\/security\/masters$/, reply: () => ({ approval_mode: 'DUAL', seats: [], invite: INVITE, can_invite: false }) },
    ])
    mount(makeMasterMe('DUAL', 'NOT_INVITED'))
    await user.click(await screen.findByRole('button', { name: 'Convidar agora' }))
    await user.type(screen.getByLabelText(/Nome completo do convidado/), 'Maria Souza')
    await user.type(screen.getByLabelText(/E-mail do convidado/), 'maria@rd.invalid')
    await user.type(screen.getByLabelText(/Seu código do autenticador/), '123456')
    await user.click(screen.getByRole('button', { name: 'Enviar convite' }))
    expect(await screen.findByRole('dialog', { name: 'Convite enviado' })).toHaveTextContent('m***@rd.invalid')
    expect(r.find('POST', /invites$/)[0].body).toEqual({ full_name: 'Maria Souza', email: 'maria@rd.invalid', totp_code: '123456' })
    // o estado vem do servidor: banner passa a "convite enviado"
    expect(await screen.findByText('Convite enviado para m***@rd.invalid — aguardando ativação')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Gerenciar', hidden: true })).toHaveAttribute('href', '/configuracoes/seguranca')
    expect(document.body.innerHTML).not.toMatch(/ativar-master|token=/)
  }, 30_000)

  it('convite pendente: banner com e-mail mascarado e "Gerenciar", sem modal', async () => {
    fetchRouter([{ match: /\/security\/masters$/, reply: () => ({ approval_mode: 'DUAL', seats: [], invite: INVITE, can_invite: false }) }])
    mount(makeMasterMe('DUAL', 'INVITE_PENDING'))
    expect(await screen.findByText('Convite enviado para m***@rd.invalid — aguardando ativação')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Gerenciar' })).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('outro Master com a configuração de segurança pendente: aviso específico, sem modal', () => {
    mount(makeMasterMe('DUAL', 'OTHER_MASTER_SETUP_PENDING'))
    expect(screen.getByText('O segundo Administrador Master ainda não concluiu a configuração de segurança.')).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('2º Master ativo: nada aparece', () => {
    mount(makeMasterMe('DUAL', 'DONE'))
    expect(screen.queryByTestId('second-master-banner')).not.toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('SINGLE: nenhum modal, banner ou texto de segundo Master — e nenhuma chamada ao servidor', () => {
    const r = fetchRouter([])
    const { container } = mount(makeMasterMe('SINGLE'))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByTestId('second-master-banner')).not.toBeInTheDocument()
    expect(container.textContent).toBe('')
    expect(document.body.textContent).not.toMatch(/segundo|2º Master/i)
    expect(r.calls).toHaveLength(0)
  })

  it('quem não é Master (ex.: financeiro) nunca vê o aviso, mesmo na dupla', () => {
    mount(makeMe({ security_setup: { ...makeMasterMe('DUAL', 'NOT_INVITED').security_setup!, is_admin_master: false } }))
    expect(screen.queryByTestId('second-master-banner')).not.toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('na própria tela de Segurança da instância nem o modal nem o banner aparecem (estado e convite já estão à vista)', () => {
    mount(makeMasterMe('DUAL', 'NOT_INVITED'), '/configuracoes/seguranca')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByTestId('second-master-banner')).not.toBeInTheDocument()
  })
})

describe('formulário de convite', () => {
  function renderForm() {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    useAuthStore.setState({ status: 'authenticated', me: makeMasterMe('DUAL'), demo: false })
    return render(
      <QueryClientProvider client={qc}>
        <InviteSecondMasterForm />
      </QueryClientProvider>,
    )
  }
  async function fill(user: ReturnType<typeof userEvent.setup>) {
    await user.type(screen.getByLabelText(/Nome completo do convidado/), 'Maria Souza')
    await user.type(screen.getByLabelText(/E-mail do convidado/), 'maria@rd.invalid')
    await user.type(screen.getByLabelText(/Seu código do autenticador/), '123456')
    await user.click(screen.getByRole('button', { name: 'Enviar convite' }))
  }

  it('valida os campos antes de enviar (nada sai sem nome, e-mail e TOTP)', async () => {
    const user = userEvent.setup()
    const r = fetchRouter([])
    renderForm()
    await user.click(screen.getByRole('button', { name: 'Enviar convite' }))
    expect(await screen.findByText('Informe o nome completo do convidado.')).toBeInTheDocument()
    expect(screen.getByText('Informe o e-mail do convidado.')).toBeInTheDocument()
    expect(screen.getByText('Informe o código de 6 dígitos do aplicativo autenticador.')).toBeInTheDocument()
    expect(r.calls).toHaveLength(0)
  })

  it('e-mail já cadastrado e TOTP inválido ficam junto do campo; o TOTP é limpo (uso único)', async () => {
    const user = userEvent.setup()
    let n = 0
    fetchRouter([
      {
        method: 'POST',
        match: /\/security\/masters\/invites$/,
        reply: () => {
          n += 1
          return n === 1
            ? jsonResponse({ error: { code: 'EMAIL_ALREADY_REGISTERED', message: 'Este e-mail já pertence a outra conta.' } }, 409)
            : jsonResponse({ error: { code: 'INVALID_MFA_CODE', message: 'Código inválido.' } }, 400)
        },
      },
    ])
    renderForm()
    await fill(user)
    expect(await screen.findByText('Este e-mail já pertence a outra conta.')).toBeInTheDocument()
    expect(screen.getByLabelText(/E-mail do convidado/)).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByLabelText(/Seu código do autenticador/)).toHaveValue('')
    await user.type(screen.getByLabelText(/Seu código do autenticador/), '654321')
    await user.click(screen.getByRole('button', { name: 'Enviar convite' }))
    expect(await screen.findByText(/Código inválido ou já utilizado/)).toBeInTheDocument()
  }, 30_000)

  it('409 de convite pendente/assentos cheios aparece como mensagem do servidor', async () => {
    const user = userEvent.setup()
    fetchRouter([
      {
        method: 'POST',
        match: /\/security\/masters\/invites$/,
        reply: () => jsonResponse({ error: { code: 'MASTER_INVITE_ALREADY_PENDING', message: 'Já existe um convite pendente para o segundo Administrador Master.' } }, 409),
      },
    ])
    renderForm()
    await fill(user)
    expect(await screen.findByRole('alert')).toHaveTextContent('Já existe um convite pendente para o segundo Administrador Master.')
  }, 30_000)
})

describe('tempo real', () => {
  it('security.status_changed recarrega /me (banner/modal reagem); o mapa de eventos invalida security e me', async () => {
    expect(invalidationsFor('security.status_changed')).toEqual(expect.arrayContaining(['me', 'security']))
    const handlers: Record<string, RealtimeHandler> = {}
    const r = fetchRouter([{ match: /\/me$/, reply: () => makeMasterMe('DUAL', 'DONE') }])
    tokenStore.set('A1')
    useAuthStore.setState({ status: 'authenticated', me: makeMasterMe('DUAL', 'INVITE_PENDING'), demo: false })
    render(
      <RealtimeContext.Provider
        value={{
          status: 'open',
          lastSyncAt: null,
          subscribe: (type, h) => {
            handlers[type] = h
            return () => undefined
          },
        }}
      >
        <SecurityStatusSync />
      </RealtimeContext.Provider>,
    )
    expect(handlers['security.status_changed']).toBeTypeOf('function')
    handlers['security.status_changed']({ type: 'security.status_changed', id: 'e1', ts: '2026-10-08T12:00:00Z', data: {} })
    await waitFor(() => expect(useAuthStore.getState().me?.security_setup?.steps.second_master).toBe('DONE'))
    expect(r.find('GET', /\/me$/)).toHaveLength(1)
  })
})
