import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RealtimeProvider } from './RealtimeProvider'
import { useRealtimeEvent, useRealtimeStatus } from './context'
import { computeBackoff } from './connection'
import { EVENT_INVALIDATIONS } from './eventMap'
import { jsonResponse, mockFetch } from '@/test/utils'
import { tokenStore } from '@/lib/http'
import { QUERY_DOMAINS } from '@/lib/queryKeys'

class MockWebSocket {
  static instances: MockWebSocket[] = []
  url: string
  onopen: ((ev: Event) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  closed = false
  constructor(url: string) {
    this.url = url
    MockWebSocket.instances.push(this)
  }
  send() {}
  close() {
    this.closed = true
  }
  serverOpen() {
    this.onopen?.(new Event('open'))
  }
  serverMessage(data: unknown) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }))
  }
  serverClose() {
    this.onclose?.(new CloseEvent('close', { code: 1006 }))
  }
}

function Probe({ onEvent }: { onEvent?: (t: string) => void }) {
  const { status, lastSyncAt } = useRealtimeStatus()
  useRealtimeEvent('*', (e) => onEvent?.(e.type))
  return (
    <p>
      status:{status} sync:{lastSyncAt ? 'sim' : 'não'}
    </p>
  )
}

function setup(onEvent?: (t: string) => void) {
  const qc = new QueryClient()
  const invalidate = vi.spyOn(qc, 'invalidateQueries').mockResolvedValue(undefined)
  const utils = render(
    <QueryClientProvider client={qc}>
      <RealtimeProvider enabled WebSocketImpl={MockWebSocket as unknown as typeof WebSocket} random={() => 0.5}>
        <Probe onEvent={onEvent} />
      </RealtimeProvider>
    </QueryClientProvider>,
  )
  return { qc, invalidate, ...utils }
}

beforeEach(() => {
  MockWebSocket.instances = []
  tokenStore.set('A1')
})
afterEach(() => {
  vi.unstubAllGlobals()
  tokenStore.clear()
})

describe('RealtimeProvider', () => {
  it('pede ticket, conecta em /ws/admin?ticket=, mapeia eventos → queries e reconecta com backoff fazendo UM resync', async () => {
    let tickets = 0
    const fetchMock = mockFetch((url, init) => {
      if (url.endsWith('/realtime/ticket') && init.method === 'POST') {
        tickets++
        return jsonResponse({ ticket: `T${tickets}`, expires_in: 30 })
      }
      return jsonResponse({}, 404)
    })
    const setIntervalSpy = vi.spyOn(globalThis, 'setInterval')
    const events: string[] = []
    const { invalidate, unmount } = setup((t) => events.push(t))

    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1))
    const ws1 = MockWebSocket.instances[0]
    expect(ws1.url).toMatch(/\/ws\/admin\?ticket=T1$/)
    expect(fetchMock.mock.calls[0][1]?.headers).toMatchObject({ Authorization: 'Bearer A1' })

    act(() => ws1.serverOpen())
    expect(screen.getByText(/status:open/)).toBeInTheDocument()
    expect(invalidate).not.toHaveBeenCalled() // primeira conexão: sem resync

    act(() => ws1.serverMessage({ type: 'screen.status', id: 'e1', ts: '2026-10-07T12:00:00Z', data: { id: 's1' } }))
    const calledKeys = invalidate.mock.calls.map(([f]) => (f as { queryKey?: unknown[] }).queryKey?.[0])
    expect(calledKeys).toEqual(EVENT_INVALIDATIONS['screen.status'])
    invalidate.mock.calls.forEach(([f]) => expect((f as { refetchType?: string }).refetchType).toBe('active'))
    expect(events).toEqual(['screen.status'])

    // Evento repetido (mesmo id) é ignorado
    invalidate.mockClear()
    act(() => ws1.serverMessage({ type: 'screen.status', id: 'e1', ts: '2026-10-07T12:00:00Z', data: {} }))
    expect(invalidate).not.toHaveBeenCalled()

    // Queda → reconexão com backoff (novo ticket) → um único refetch das queries ativas
    act(() => ws1.serverClose())
    expect(screen.getByText(/status:reconnecting/)).toBeInTheDocument()
    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(2), { timeout: 3000 })
    const ws2 = MockWebSocket.instances[1]
    expect(ws2.url).toMatch(/ticket=T2$/)
    act(() => ws2.serverOpen())
    expect(invalidate).toHaveBeenCalledTimes(1)
    expect(invalidate).toHaveBeenCalledWith({ refetchType: 'active' })

    // Sem polling: nenhum setInterval foi criado
    // (o waitFor do Testing Library usa setInterval próprio — "checkRealTimersCallback" — e é ignorado)
    const appIntervals = setIntervalSpy.mock.calls.filter(([fn]) => (fn as { name?: string }).name !== 'checkRealTimersCallback')
    expect(appIntervals).toEqual([])

    unmount()
    expect(ws2.closed).toBe(true)
  })

  it('ticket indisponível (404) → status "unavailable" sem novas tentativas', async () => {
    const fetchMock = mockFetch(() => jsonResponse({ error: { code: 'NOT_FOUND', message: 'x' } }, 404))
    setup()
    await waitFor(() => expect(screen.getByText(/status:unavailable/)).toBeInTheDocument())
    await new Promise((r) => setTimeout(r, 900))
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(MockWebSocket.instances).toHaveLength(0)
  })

  it('troca de identidade (impersonation) reabre o WS com NOVO ticket e invalida o cache', async () => {
    let tickets = 0
    mockFetch((url, init) => {
      if (url.endsWith('/realtime/ticket') && init.method === 'POST') {
        tickets++
        return jsonResponse({ ticket: `T${tickets}`, expires_in: 30 })
      }
      return jsonResponse({}, 404)
    })
    const qc = new QueryClient()
    const invalidate = vi.spyOn(qc, 'invalidateQueries').mockResolvedValue(undefined)
    const rnd = () => 0.5 // estável entre renders (é dependência do efeito)
    const tree = (identity: string) => (
      <QueryClientProvider client={qc}>
        <RealtimeProvider enabled identity={identity} WebSocketImpl={MockWebSocket as unknown as typeof WebSocket} random={rnd}>
          <Probe />
        </RealtimeProvider>
      </QueryClientProvider>
    )
    const { rerender, unmount } = render(tree('real'))
    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(1))
    const ws1 = MockWebSocket.instances[0]
    act(() => ws1.serverOpen())
    expect(invalidate).not.toHaveBeenCalled()

    // Início da impersonation: /me passa a ser de outro usuário
    tokenStore.set('IMP')
    rerender(tree('impersonado'))
    await waitFor(() => expect(MockWebSocket.instances).toHaveLength(2))
    expect(ws1.closed).toBe(true)
    expect(MockWebSocket.instances[1].url).toMatch(/ticket=T2$/)
    expect(invalidate).toHaveBeenCalledWith({ refetchType: 'active' })

    // Mesma identidade (re-render) não reconecta
    rerender(tree('impersonado'))
    await new Promise((r) => setTimeout(r, 50))
    expect(MockWebSocket.instances).toHaveLength(2)
    unmount()
  })

  it('todo evento do contrato §5 invalida apenas domínios conhecidos de queryKey', () => {
    for (const domains of Object.values(EVENT_INVALIDATIONS)) {
      domains.forEach((d) => expect(QUERY_DOMAINS).toContain(d))
    }
  })
})

describe('computeBackoff', () => {
  it('cresce exponencialmente com jitter e respeita o teto de 30 s', () => {
    expect(computeBackoff(0, 1000, 30000, () => 0)).toBe(500)
    expect(computeBackoff(0, 1000, 30000, () => 1)).toBe(1000)
    expect(computeBackoff(3, 1000, 30000, () => 0)).toBe(4000)
    expect(computeBackoff(10, 1000, 30000, () => 1)).toBe(30000)
    expect(computeBackoff(10, 1000, 30000, () => 0)).toBe(15000)
  })
})
