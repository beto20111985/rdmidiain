/**
 * Fumaça: cada página do FE-OPS monta com respostas vazias do servidor e mostra um estado
 * explícito (vazio/explicação) — sem quebrar e sem dado inventado.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'
import type { ComponentType } from 'react'
import { PERMISSIONS } from '@/lib/permissions'
import { renderAt, routeFetch, setMe } from '@/test/opsTestUtils'
import ComodatoPage from '../plans/ComodatoPage'
import PartnerPlansPage from '../plans/PartnerPlansPage'
import ContractsPage from '../contracts/ContractsPage'
import InternalContentPage from '../content/InternalContentPage'
import EquipmentListPage from '../equipment/EquipmentListPage'
import EquipmentCreatePage from '../equipment/EquipmentCreatePage'
import MaintenanceListPage from '../maintenance/MaintenanceListPage'
import InstallationsListPage from '../installations/InstallationsListPage'
import OnboardingQueuePage from '../onboarding/OnboardingQueuePage'
import ScreenCreatePage from '../screens/ScreenCreatePage'
import MyScreensPage from '../unit/MyScreensPage'

// Fluxos de várias etapas digitados tecla a tecla: limite explícito por teste (determinístico; o padrão de
// 5 s estoura quando outros agentes usam a CPU do container ao mesmo tempo).
vi.setConfig({ testTimeout: 30_000 })

const empty = { items: [], total: 0, page: 1, page_size: 25 }

beforeEach(() => {
  setMe([...PERMISSIONS])
  routeFetch([
    { path: /\/api\/v1\/(comodato-agreements|partner-plans|contracts\/templates|equipment|maintenance|installations|onboarding|screens|establishments|taxonomy\/nodes|taxonomy\/segments|organizations|creatives)$/, reply: () => empty },
    { path: '/equipment/summary', reply: () => ({ serial_by_type_status: {}, bulk_in_warehouse: [] }) },
    { path: '/internal-content/fallback', reply: () => ({ items: [] }) },
    { path: '/users/assignable', reply: () => ({ items: [] }) },
  ])
})
afterEach(() => vi.unstubAllGlobals())

const CASES: Array<[string, ComponentType, string, RegExp]> = [
  ['Comodato', ComodatoPage, '/estabelecimentos/comodato', /Nenhum acordo de comodato/],
  ['Planos do parceiro', PartnerPlansPage, '/estabelecimentos/planos', /Nenhum plano cadastrado/],
  ['Contratos', ContractsPage, '/contratos', /Nenhuma versão cadastrada/],
  ['Conteúdo interno', InternalContentPage, '/conteudo-interno', /Escolha um estabelecimento/],
  ['Estoque', EquipmentListPage, '/estoque', /Nenhum item no estoque/],
  ['Entrada no estoque', EquipmentCreatePage, '/estoque/novo', /Registrar entrada/],
  ['Manutenção', MaintenanceListPage, '/estoque/manutencao', /Nenhuma ordem de manutenção/],
  ['Instalações', InstallationsListPage, '/estoque/instalacoes', /Nenhuma instalação/],
  ['Cadastros', OnboardingQueuePage, '/estabelecimentos/cadastros', /Nenhum cadastro de parceiro/],
  ['Adicionar tela', ScreenCreatePage, '/telas/nova', /Cadastrar tela/],
  ['Minhas telas', MyScreensPage, '/unidade/telas', /Nenhuma tela instalada ainda/],
]

describe('páginas do FE-OPS montam com estado vazio explícito', () => {
  it.each(CASES)('%s', async (_name, Page, path, text) => {
    renderAt(<Page />, path, path)
    expect((await screen.findAllByText(text)).length).toBeGreaterThan(0)
    // um único <h1> (do header) — a página não cria outro
    expect(screen.queryAllByRole('heading', { level: 1 }).length).toBeLessThanOrEqual(1)
  })
})
