// Deploy sem queda (AÇÃO 007): depois que uma versão nova do painel entra no ar, os chunks
// (import dinâmico de rotas) da versão anterior deixam de existir no servidor. Quem estava
// com o painel aberto receberia erro ao abrir uma rota ainda não carregada; em vez disso,
// recarrega a página UMA vez (a trava evita laço se o problema não for a troca de versão).

const RELOAD_KEY = 'rd:chunk-reload-at'
const MIN_INTERVAL_MS = 10_000

interface ChunkReloadEnv {
  addEventListener: (type: 'vite:preloadError', listener: (event: Event) => void) => void
  reload: () => void
  storage: Pick<Storage, 'getItem' | 'setItem'> | null
  now: () => number
}

export function installChunkReloadGuard(env: ChunkReloadEnv): void {
  env.addEventListener('vite:preloadError', (event) => {
    let last = 0
    try {
      last = Number(env.storage?.getItem(RELOAD_KEY) ?? 0)
    } catch {
      /* armazenamento indisponível: recarrega mesmo assim */
    }
    if (env.now() - last < MIN_INTERVAL_MS) return // já recarregou há pouco: deixa o erro aparecer
    try {
      env.storage?.setItem(RELOAD_KEY, String(env.now()))
    } catch {
      /* idem */
    }
    event.preventDefault()
    env.reload()
  })
}

export function installBrowserChunkReloadGuard(): void {
  let storage: Storage | null = null
  try {
    storage = window.sessionStorage
  } catch {
    storage = null
  }
  installChunkReloadGuard({
    addEventListener: (type, listener) => window.addEventListener(type, listener),
    reload: () => window.location.reload(),
    storage,
    now: () => Date.now(),
  })
}
