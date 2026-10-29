/**
 * Tema (E2 #975–976, P001 §5): escuro e claro, escolha persistente do usuário.
 * - localStorage guarda SOMENTE a preferência de tema (chave `rd-theme`), lida também por
 *   public/theme-init.js antes da pintura (sem flash).
 * - Quando autenticado, a preferência é sincronizada com `user.theme` (PATCH /me) — a do
 *   servidor prevalece ao carregar /me, para acompanhar o usuário entre dispositivos.
 */
import { create } from 'zustand'

export type ThemePreference = 'dark' | 'light' | 'system'
export type ResolvedTheme = 'dark' | 'light'

export const THEME_STORAGE_KEY = 'rd-theme'

export function isThemePreference(v: unknown): v is ThemePreference {
  return v === 'dark' || v === 'light' || v === 'system'
}

export function readStoredPreference(): ThemePreference {
  try {
    const raw = window.localStorage.getItem(THEME_STORAGE_KEY)
    if (isThemePreference(raw)) return raw
    if (raw) {
      // Migração do formato antigo (zustand persist: {"state":{"theme":"..."}})
      const parsed = JSON.parse(raw) as { state?: { theme?: unknown } }
      const legacy = parsed?.state?.theme
      if (isThemePreference(legacy)) {
        window.localStorage.setItem(THEME_STORAGE_KEY, legacy)
        return legacy
      }
    }
  } catch {
    /* storage indisponível ou JSON inválido */
  }
  return 'dark'
}

function systemPrefersLight(): boolean {
  try {
    return window.matchMedia('(prefers-color-scheme: light)').matches
  } catch {
    return false
  }
}

export function resolveTheme(pref: ThemePreference): ResolvedTheme {
  if (pref === 'system') return systemPrefersLight() ? 'light' : 'dark'
  return pref
}

export function applyTheme(resolved: ResolvedTheme) {
  const root = document.documentElement
  root.setAttribute('data-theme', resolved)
  const meta = document.querySelector('meta[name="theme-color"]')
  meta?.setAttribute('content', resolved === 'light' ? '#f2f5f9' : '#05070a')
}

type ThemeSync = (pref: ThemePreference) => void
let remoteSync: ThemeSync | null = null
/** Registrado pelo AuthProvider quando há sessão: envia a preferência ao servidor. */
export function setThemeRemoteSync(fn: ThemeSync | null) {
  remoteSync = fn
}

interface ThemeState {
  preference: ThemePreference
  resolved: ResolvedTheme
  /** Mudança feita pelo usuário (persiste localmente e sincroniza com a API). */
  setPreference: (pref: ThemePreference) => void
  /** Aplica a preferência vinda do servidor (sem reenviar). */
  applyServerPreference: (pref: ThemePreference) => void
  /** Reavalia "system" quando o SO muda. */
  refreshSystem: () => void
}

function persist(pref: ThemePreference) {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, pref)
  } catch {
    /* ignora */
  }
}

const initialPref = typeof window !== 'undefined' ? readStoredPreference() : 'dark'

export const useThemeStore = create<ThemeState>((set, get) => ({
  preference: initialPref,
  resolved: typeof window !== 'undefined' ? resolveTheme(initialPref) : 'dark',
  setPreference: (pref) => {
    const resolved = resolveTheme(pref)
    applyTheme(resolved)
    persist(pref)
    set({ preference: pref, resolved })
    remoteSync?.(pref)
  },
  applyServerPreference: (pref) => {
    if (pref === get().preference) return
    const resolved = resolveTheme(pref)
    applyTheme(resolved)
    persist(pref)
    set({ preference: pref, resolved })
  },
  refreshSystem: () => {
    const { preference } = get()
    if (preference !== 'system') return
    const resolved = resolveTheme('system')
    applyTheme(resolved)
    set({ resolved })
  },
}))

if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
  try {
    window
      .matchMedia('(prefers-color-scheme: light)')
      .addEventListener?.('change', () => useThemeStore.getState().refreshSystem())
  } catch {
    /* navegadores antigos */
  }
}
