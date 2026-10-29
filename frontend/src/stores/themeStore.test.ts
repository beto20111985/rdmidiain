import { afterEach, describe, expect, it, vi } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'
import { readStoredPreference, setThemeRemoteSync, THEME_STORAGE_KEY, useThemeStore } from './themeStore'

function runThemeInit() {
  const code = fs.readFileSync(path.resolve(__dirname, '../../public/theme-init.js'), 'utf8')
  // Executa o script como o navegador faria antes da pintura.
  new Function(code)()
}

function mockSystemLight(light: boolean) {
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({
    matches: light && q.includes('light'),
    media: q,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    onchange: null,
    dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia
}

const originalMatchMedia = window.matchMedia

afterEach(() => {
  window.matchMedia = originalMatchMedia
  setThemeRemoteSync(null)
  document.documentElement.setAttribute('data-theme', 'dark')
})

describe('tema persistente', () => {
  it('setPreference aplica data-theme, grava SOMENTE a preferência no localStorage e sincroniza com o perfil', () => {
    const sync = vi.fn()
    setThemeRemoteSync(sync)
    useThemeStore.getState().setPreference('light')
    expect(document.documentElement.getAttribute('data-theme')).toBe('light')
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('light')
    expect(window.localStorage.length).toBe(1)
    expect(sync).toHaveBeenCalledWith('light')
  })

  it('theme-init.js aplica o tema salvo antes do React (sem flash)', () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, 'light')
    document.documentElement.setAttribute('data-theme', 'dark')
    runThemeInit()
    expect(document.documentElement.getAttribute('data-theme')).toBe('light')
  })

  it('theme-init.js resolve "system" pela preferência do sistema e usa escuro por padrão', () => {
    mockSystemLight(true)
    window.localStorage.setItem(THEME_STORAGE_KEY, 'system')
    runThemeInit()
    expect(document.documentElement.getAttribute('data-theme')).toBe('light')

    window.localStorage.clear()
    runThemeInit()
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
  })

  it('preferência do servidor (user.theme) é aplicada sem reenviar ao servidor', () => {
    const sync = vi.fn()
    setThemeRemoteSync(sync)
    useThemeStore.getState().setPreference('dark')
    sync.mockClear()
    useThemeStore.getState().applyServerPreference('light')
    expect(document.documentElement.getAttribute('data-theme')).toBe('light')
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('light')
    expect(sync).not.toHaveBeenCalled()
  })

  it('migra o formato antigo do zustand persist', () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, JSON.stringify({ state: { theme: 'light' }, version: 0 }))
    expect(readStoredPreference()).toBe('light')
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('light')
  })
})
