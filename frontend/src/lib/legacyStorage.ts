/**
 * Migração de segurança (A3 §8, E2 #864, P001 §98): a versão anterior do painel guardava
 * access/refresh token e o estado de login no localStorage. Esses valores são apagados no
 * boot — tokens agora vivem só em memória (access) e em cookie HttpOnly (refresh).
 */
export const LEGACY_STORAGE_KEYS = ['rd_access_token', 'rd_refresh_token', 'rd-auth'] as const

export function purgeLegacyAuthStorage(storage: Storage | undefined = globalThis.localStorage): number {
  if (!storage) return 0
  let removed = 0
  for (const key of LEGACY_STORAGE_KEYS) {
    try {
      if (storage.getItem(key) !== null) {
        storage.removeItem(key)
        removed++
      }
    } catch {
      /* armazenamento indisponível */
    }
  }
  return removed
}
