import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, screen, waitFor } from '@testing-library/react'
import { QueryClient } from '@tanstack/react-query'
import { BrandingSync } from '../BrandingSync'
import { BRANDING_QUERY_KEY } from '../query'
import { DEFAULT_BRAND, resolveBrand, useBrandStore } from '../brand'
import { LogoMark } from '@/components/layout/logo'
import { AuthLayout } from '@/features/auth/components/AuthLayout'
import { renderWithProviders } from '@/test/utils'
import { jsonResponse, mockApi } from '@/test/adminTestUtils'

vi.setConfig({ testTimeout: 20_000 })

const RD_DEFAULT = {
  instance_slug: 'rd',
  display_name: 'RD Mídia Indoor',
  is_white_label: false,
  primary_color: '#0056B3',
  accent_color: '#0B84FF',
  has_logo: false,
  logo_url: null,
  support_email: null,
  support_phone: null,
}

const root = () => document.documentElement.style
const favicon = () => document.querySelector<HTMLLinkElement>('link[rel="icon"]')

function renderBrandingUI() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = renderWithProviders(
    <>
      <BrandingSync />
      <AuthLayout title="Entrar">
        <p>formulário</p>
      </AuthLayout>
      <LogoMark />
    </>,
    { queryClient: qc },
  )
  return { qc, ...utils }
}

beforeEach(() => {
  useBrandStore.setState({ brand: DEFAULT_BRAND })
  ;['--action', '--action-hover', '--brand-blue'].forEach((v) => root().removeProperty(v))
  document.head.querySelectorAll('link[rel="icon"]').forEach((l) => l.remove())
  const link = document.createElement('link')
  link.rel = 'icon'
  link.type = 'image/svg+xml'
  link.href = '/favicon.svg'
  document.head.appendChild(link)
  document.title = 'RD Mídia Indoor'
})
afterEach(() => vi.unstubAllGlobals())

describe('Marca da instância (white-label) no painel', () => {
  it('padrão RD: nada é sobrescrito — logo SVG original, título, favicon e tokens idênticos (regressão)', async () => {
    const { calls } = mockApi((c) => (c.path === '/instance/branding' ? jsonResponse(RD_DEFAULT) : undefined))
    renderBrandingUI()
    await waitFor(() => expect(calls.some((c) => c.path === '/instance/branding')).toBe(true))
    // Leitura pública: sem Bearer e sem contexto
    const call = calls.find((c) => c.path === '/instance/branding')!
    expect(call.headers.Authorization).toBeUndefined()
    expect(call.headers['X-RD-Context']).toBeUndefined()
    await waitFor(() => expect(document.title).toBe('Entrar · RD Mídia Indoor'))
    expect(useBrandStore.getState().brand.isDefault).toBe(true)
    expect(screen.getByText('MÍDIA')).toBeInTheDocument()
    expect(screen.getByText('INDOOR')).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'RD Mídia Indoor' }).tagName.toLowerCase()).toBe('svg')
    expect(root().getPropertyValue('--action')).toBe('')
    expect(root().getPropertyValue('--brand-blue')).toBe('')
    expect(favicon()?.getAttribute('href')).toBe('/favicon.svg')
    expect(favicon()?.getAttribute('type')).toBe('image/svg+xml')
    expect(window.localStorage.length).toBe(0)
  })

  it('marca configurada: nome, logo, favicon e cores aplicados SOMENTE pelos tokens do DS', async () => {
    mockApi((c) =>
      c.path === '/instance/branding'
        ? jsonResponse({ ...RD_DEFAULT, display_name: 'Rede Sul Mídia', is_white_label: true, primary_color: '#7A1F5C', accent_color: '#D9480F', has_logo: true, logo_url: '/api/v1/instance/logo' })
        : undefined,
    )
    renderBrandingUI()
    await waitFor(() => expect(document.title).toBe('Entrar · Rede Sul Mídia'))
    expect(root().getPropertyValue('--action')).toBe('122 31 92')
    expect(root().getPropertyValue('--action-hover')).toBe('104 26 78')
    expect(root().getPropertyValue('--brand-blue')).toBe('#d9480f')
    expect(favicon()?.getAttribute('href')).toMatch(/^\/api\/v1\/instance\/logo\?v=\d+$/)
    expect(screen.queryByText('MÍDIA')).not.toBeInTheDocument()
    expect(screen.getAllByText('Rede Sul Mídia').length).toBeGreaterThan(0)
    expect(screen.getByRole('img', { name: 'Rede Sul Mídia' })).toHaveAttribute('src', expect.stringMatching(/^\/api\/v1\/instance\/logo\?v=/))
  })

  it('cor com contraste insuficiente é recusada: mantém o token padrão RD', async () => {
    mockApi((c) =>
      c.path === '/instance/branding'
        ? jsonResponse({ ...RD_DEFAULT, display_name: 'Rede Clara', primary_color: '#5BCEFF', accent_color: '#3FA7FF' })
        : undefined,
    )
    renderBrandingUI()
    await waitFor(() => expect(useBrandStore.getState().brand.name).toBe('Rede Clara'))
    expect(useBrandStore.getState().brand.rejected).toEqual(['primary', 'accent'])
    expect(root().getPropertyValue('--action')).toBe('')
    expect(root().getPropertyValue('--brand-blue')).toBe('')
    expect(resolveBrand({ ...RD_DEFAULT, primary_color: '#5BCEFF' }).primary).toBeNull()
  })

  it('erro de rede → marca RD padrão (e remove tokens antigos)', async () => {
    root().setProperty('--action', '1 2 3')
    vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(new TypeError('Failed to fetch'))))
    renderBrandingUI()
    await waitFor(() => expect(root().getPropertyValue('--action')).toBe(''))
    expect(useBrandStore.getState().brand).toEqual(DEFAULT_BRAND)
    expect(document.title).toBe('Entrar · RD Mídia Indoor')
    expect(favicon()?.getAttribute('href')).toBe('/favicon.svg')
    expect(screen.getByText('MÍDIA')).toBeInTheDocument()
  })

  it('invalidar a query (ex.: ao salvar o white-label) atualiza a aparência sem recarregar — inclusive após limpar o cache', async () => {
    let name = 'Rede Sul Mídia'
    mockApi((c) => (c.path === '/instance/branding' ? jsonResponse({ ...RD_DEFAULT, display_name: name }) : undefined))
    const { qc } = renderBrandingUI()
    await waitFor(() => expect(document.title).toBe('Entrar · Rede Sul Mídia'))
    name = 'Rede Norte Mídia'
    await act(async () => {
      await qc.invalidateQueries({ queryKey: BRANDING_QUERY_KEY })
    })
    await waitFor(() => expect(document.title).toBe('Entrar · Rede Norte Mídia'))
    // logout/troca de contexto limpam o cache: o observador recria a query e segue recebendo invalidações
    name = 'Rede Leste Mídia'
    act(() => qc.clear())
    await waitFor(() => expect(document.title).toBe('Entrar · Rede Leste Mídia'))
    expect(screen.getAllByText('Rede Leste Mídia').length).toBeGreaterThan(0)
  })
})
