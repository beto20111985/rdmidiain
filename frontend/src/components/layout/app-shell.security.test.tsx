/**
 * AppShell × segurança da instância (SPEC AÇÃO 003 §11.3–§11.5): o banner persistente e o modal do
 * 2º Master vivem no AppShell, só na aprovação DUPLA para o titular de assento; na ÚNICA o painel não
 * contém NENHUM texto sobre segundo Master.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { Route, Routes } from 'react-router-dom'
import { AppShell } from './app-shell'
import { useAuthStore } from '@/stores/authStore'
import { useContextStore } from '@/stores/contextStore'
import { useSecurityUiStore } from '@/features/security/uiStore'
import { tokenStore } from '@/lib/http'
import { fetchRouter } from '@/components/finance/testing'
import { makeMasterMe } from '@/test/securityFixtures'
import { renderWithProviders } from '@/test/utils'
import type { Me } from '@/features/auth/types'

function renderShell(me: Me) {
  useAuthStore.setState({ status: 'authenticated', me, demo: false })
  useContextStore.getState().syncFromMe(me.contexts)
  fetchRouter([{ match: /\/notifications\/unread-count$/, reply: () => ({ count: 0 }) }])
  return renderWithProviders(
    <Routes>
      <Route element={<AppShell />}>
        <Route path="/visao-geral" element={<p>conteúdo da página</p>} />
      </Route>
    </Routes>,
    { route: '/visao-geral' },
  )
}

beforeEach(() => {
  tokenStore.clear()
  useContextStore.getState().reset()
  useSecurityUiStore.getState().reset()
})
afterEach(() => vi.unstubAllGlobals())

describe('AppShell — aviso do 2º Master', () => {
  it('DUPLA + Master + não convidado: banner persistente dentro do conteúdo e modal da sessão', async () => {
    renderShell(makeMasterMe('DUAL', 'NOT_INVITED'))
    expect(await screen.findByRole('dialog', { name: 'Configure o segundo Administrador Master' })).toBeInTheDocument()
    const banner = screen.getByTestId('second-master-banner')
    expect(banner).toHaveTextContent('Configuração de segurança pendente')
    expect(screen.getByRole('main')).toContainElement(banner)
    expect(screen.getByText('conteúdo da página', { selector: 'p' })).toBeInTheDocument()
  })

  it('ÚNICA: o painel inteiro fica sem banner, modal ou qualquer menção a segundo Master', () => {
    renderShell(makeMasterMe('SINGLE'))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByTestId('second-master-banner')).not.toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/segundo|2º Master/i)
  })

  it('DUPLA com o 2º Master ativo: sem aviso', () => {
    renderShell(makeMasterMe('DUAL', 'DONE'))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByTestId('second-master-banner')).not.toBeInTheDocument()
  })
})
