import { describe, expect, it, vi } from 'vitest'
import { installChunkReloadGuard } from './chunkReload'

function setup(now: number, stored: string | null = null) {
  let listener: ((e: Event) => void) | null = null
  const data = new Map<string, string>()
  if (stored !== null) data.set('rd:chunk-reload-at', stored)
  const env = {
    addEventListener: (_t: 'vite:preloadError', l: (e: Event) => void) => {
      listener = l
    },
    reload: vi.fn(),
    storage: { getItem: (k: string) => data.get(k) ?? null, setItem: (k: string, v: string) => void data.set(k, v) },
    now: () => now,
  }
  installChunkReloadGuard(env)
  const event = { preventDefault: vi.fn() } as unknown as Event
  return { fire: () => listener?.(event), env, event, data }
}

describe('installChunkReloadGuard', () => {
  it('recarrega quando um chunk da versão anterior some', () => {
    const t = setup(100_000)
    t.fire()
    expect(t.env.reload).toHaveBeenCalledTimes(1)
    expect(t.event.preventDefault).toHaveBeenCalled()
    expect(t.data.get('rd:chunk-reload-at')).toBe('100000')
  })

  it('não entra em laço: recarga recente deixa o erro seguir', () => {
    const t = setup(100_000, '95000')
    t.fire()
    expect(t.env.reload).not.toHaveBeenCalled()
    expect(t.event.preventDefault).not.toHaveBeenCalled()
  })

  it('funciona sem armazenamento disponível', () => {
    let listener: ((e: Event) => void) | null = null
    const reload = vi.fn()
    installChunkReloadGuard({
      addEventListener: (_t, l) => {
        listener = l
      },
      reload,
      storage: null,
      now: () => 100_000,
    })
    listener!({ preventDefault: vi.fn() } as unknown as Event)
    expect(reload).toHaveBeenCalledTimes(1)
  })
})
