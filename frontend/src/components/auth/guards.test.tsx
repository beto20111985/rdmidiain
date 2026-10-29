import { beforeEach, describe, expect, it } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { Route, Routes } from 'react-router-dom'
import { Can, RequireAuth, RequirePermission } from './guards'
import { RouteGuard } from '@/app/router'
import { APP_ROUTES, type AppRoute } from '@/app/routes'
import { useAuthStore } from '@/stores/authStore'
import { useContextStore } from '@/stores/contextStore'
import { makeMe, renderWithProviders } from '@/test/utils'

function signIn(permissions: string[], contexts = makeMe().contexts) {
  const me = makeMe({ permissions, contexts })
  useAuthStore.setState({ status: 'authenticated', me, demo: false })
  useContextStore.getState().syncFromMe(contexts)
}

beforeEach(() => {
  useContextStore.getState().reset()
  useAuthStore.setState({ status: 'anonymous', me: null, demo: false, notice: null })
})

describe('RequirePermission / Can', () => {
  it('sem a permissão mostra o estado "sem permissão" do DS com orientação e o código', () => {
    signIn(['campaigns.view'])
    renderWithProviders(
      <RequirePermission permission="finance.approve">
        <p>conteúdo financeiro</p>
      </RequirePermission>,
    )
    expect(screen.queryByText('conteúdo financeiro')).not.toBeInTheDocument()
    expect(screen.getByText('Você não tem acesso a esta área')).toBeInTheDocument()
    expect(screen.getByText('finance.approve')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Ir para o início' })).toBeInTheDocument()
  })

  it('com a permissão renderiza o conteúdo', () => {
    signIn(['finance.approve'])
    renderWithProviders(
      <RequirePermission permission="finance.approve">
        <p>conteúdo financeiro</p>
      </RequirePermission>,
    )
    expect(screen.getByText('conteúdo financeiro')).toBeInTheDocument()
  })

  it('<Can> esconde ações sem permissão', () => {
    signIn(['campaigns.view'])
    renderWithProviders(
      <>
        <Can permission="campaigns.approve">
          <button type="button">Aprovar</button>
        </Can>
        <Can permission="campaigns.view">
          <button type="button">Ver</button>
        </Can>
      </>,
    )
    expect(screen.queryByRole('button', { name: 'Aprovar' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Ver' })).toBeInTheDocument()
  })
})

describe('RequireAuth (sessão real)', () => {
  it('anônimo é levado ao login preservando o destino', () => {
    renderWithProviders(
      <Routes>
        <Route path="/login" element={<p>tela de login</p>} />
        <Route
          path="/telas"
          element={
            <RequireAuth>
              <p>painel</p>
            </RequireAuth>
          }
        />
      </Routes>,
      { route: '/telas' },
    )
    expect(screen.getByText('tela de login')).toBeInTheDocument()
  })

  it('setup_required (primeiro acesso pendente) leva ao assistente /primeiro-acesso', () => {
    useAuthStore.setState({ status: 'setup_required' })
    renderWithProviders(
      <Routes>
        <Route path="/primeiro-acesso" element={<p>assistente de primeiro acesso</p>} />
        <Route path="/x" element={<RequireAuth>painel</RequireAuth>} />
      </Routes>,
      { route: '/x' },
    )
    expect(screen.getByText('assistente de primeiro acesso')).toBeInTheDocument()
    expect(screen.queryByText('painel')).not.toBeInTheDocument()
  })
})

describe('RouteGuard (rotas de routes.ts)', () => {
  const screensRoute = APP_ROUTES.find((r) => r.id === 'rd.screens') as AppRoute

  it('sem permissão da rota → NoPermission', () => {
    signIn(['campaigns.view'])
    renderWithProviders(<RouteGuard route={screensRoute} />)
    expect(screen.getByText('screens.view')).toBeInTheDocument()
  })

  // Todas as rotas reais já têm `load`: os dois testes abaixo usam rotas SINTÉTICAS sem `load`
  // para preservar a intenção (NotImplementedYet para rota sem página; troca automática de contexto).
  const unbuiltRoute: AppRoute = { ...screensRoute, id: 'test.unbuilt', load: undefined }

  it('página ainda não construída → NotImplementedYet claramente identificado, sem dados', () => {
    signIn(['screens.view'])
    renderWithProviders(<RouteGuard route={unbuiltRoute} />)
    const marker = screen.getByTestId('not-implemented-yet')
    expect(marker).toHaveTextContent('Página em construção')
    expect(marker).toHaveTextContent('Nenhum dado é exibido aqui')
  })

  it('rota de outro contexto troca automaticamente para o contexto compatível do usuário', async () => {
    signIn(
      ['dashboard.view', 'campaigns.view'],
      [
        { key: 'est:1', label: 'Minha unidade — X', kind: 'ESTABLISHMENT', org_id: '1' },
        { key: 'adv:1', label: 'Minhas campanhas — X', kind: 'ADVERTISER', org_id: '1' },
      ],
    )
    expect(useContextStore.getState().activeKey).toBe('est:1')
    const advDashboard: AppRoute = { ...APP_ROUTES.find((r) => r.id === 'adv.dashboard')!, load: undefined }
    renderWithProviders(<RouteGuard route={advDashboard} />)
    await waitFor(() => expect(useContextStore.getState().activeKey).toBe('adv:1'))
    expect(await screen.findByTestId('not-implemented-yet')).toBeInTheDocument()
  })

  it('rota sem nenhum contexto compatível → aviso de indisponibilidade', () => {
    signIn(['dashboard.view'])
    const estDashboard = APP_ROUTES.find((r) => r.id === 'est.dashboard')!
    renderWithProviders(<RouteGuard route={estDashboard} />)
    expect(screen.getByText('Esta página não está disponível no seu acesso')).toBeInTheDocument()
  })
})
