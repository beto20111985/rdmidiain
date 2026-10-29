import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as React from 'react'
import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MediaUploader } from '@/components/campaigns/media-uploader'
import { RealtimeContext, type RealtimeHandler } from '@/lib/realtime/context'
import { toApiError } from '@/lib/errors'
import { jsonResponse, mockFetch, renderWithProviders } from '@/test/utils'
import type { MediaFile } from '../lib/types'
import { signInAs } from './fixtures'

const uploadMock = vi.hoisted(() => vi.fn())
vi.mock('@/lib/upload', () => ({ uploadWithProgress: uploadMock }))

const LIMITS = {
  image: { formats: ['JPG', 'PNG', 'GIF'], extensions: ['gif', 'jpeg', 'jpg', 'png'], max_bytes: 10 * 1024 * 1024 },
  video: { formats: ['MP4'], extensions: ['mp4', 'webm'], max_bytes: 200 * 1024 * 1024, audio: 'removido automaticamente', output: 'MP4' },
  resolutions: [],
  not_allowed: [],
}

function media(over: Partial<MediaFile> = {}): MediaFile {
  return {
    id: 'm1',
    org_id: 'org-1',
    name: 'video.mp4',
    original_filename: 'video.mp4',
    origin: 'UPLOAD',
    kind: 'VIDEO',
    status: 'UPLOADED',
    status_code: null,
    status_message: null,
    progress_pct: 0,
    attempts: 0,
    extension_mismatch: false,
    source: { format: 'mp4', mime: 'video/mp4', size_bytes: 1000, sha256: 'a'.repeat(64), width: 1920, height: 1080, duration_ms: 30000, codec: 'h264', fps_milli: 30000, has_audio: true },
    output: null,
    has_preview: false,
    content_url: null,
    preview_url: null,
    created_by: 'u1',
    created_at: '2099-01-01T12:00:00Z',
    updated_at: '2099-01-01T12:00:00Z',
    ready_at: null,
    ...over,
  }
}

/** Provider de tempo real controlável: o teste emite eventos `media.processing`. */
function renderUploader(props: React.ComponentProps<typeof MediaUploader> = {}) {
  const handlers = new Map<string, Set<RealtimeHandler>>()
  const subscribe = (type: string, h: RealtimeHandler) => {
    const set = handlers.get(type) ?? new Set()
    set.add(h)
    handlers.set(type, set)
    return () => set.delete(h)
  }
  const emit = (data: Record<string, unknown>) =>
    act(() => handlers.get('media.processing')?.forEach((h) => h({ type: 'media.processing', id: `e${Math.random()}`, ts: '', data })))
  renderWithProviders(
    <RealtimeContext.Provider value={{ status: 'open', lastSyncAt: null, subscribe }}>
      <MediaUploader orgId="org-1" {...props} />
    </RealtimeContext.Provider>,
  )
  return { emit }
}

beforeEach(() => {
  signInAs('advertiser')
  uploadMock.mockReset()
})

describe('upload de mídia', () => {
  it('progresso REAL de envio → processando (evento WS) → pronto', async () => {
    const fetchMock = mockFetch((url) => {
      if (url.endsWith('/media/limits')) return jsonResponse(LIMITS)
      if (url.endsWith('/media/m1')) return jsonResponse(media({ status: 'READY' }))
      return jsonResponse({})
    })
    let resolveUpload: (m: MediaFile) => void = () => undefined
    let reportProgress: (f: number) => void = () => undefined
    uploadMock.mockImplementation(({ onProgress, form }: { onProgress: (f: number) => void; form: FormData }) => {
      reportProgress = onProgress
      expect(form.get('org_id')).toBe('org-1')
      expect([...form.keys()].indexOf('org_id')).toBeLessThan([...form.keys()].indexOf('file'))
      return new Promise<MediaFile>((r) => (resolveUpload = r))
    })
    const onReady = vi.fn()
    const { emit } = renderUploader({ onReady })
    const user = userEvent.setup()
    const file = new File([new Uint8Array(1000)], 'video.mp4', { type: 'video/mp4' })
    await user.upload(screen.getByLabelText('Enviar mídia'), file)

    await waitFor(() => expect(uploadMock).toHaveBeenCalledTimes(1))
    act(() => reportProgress(0.42))
    expect(await screen.findByText(/Enviando 42%/)).toBeInTheDocument()
    expect(screen.getByRole('progressbar', { name: 'Envio de video.mp4' })).toBeInTheDocument()

    await act(async () => resolveUpload(media({ status: 'UPLOADED' })))
    expect(await screen.findByText('Processando')).toBeInTheDocument()
    expect(screen.getByText(/Na fila de processamento do servidor/)).toBeInTheDocument()

    emit({ id: 'm1', status: 'PROCESSING', progress: 55, stage: 'transcode' })
    expect(await screen.findByText(/Processando no servidor: 55%/)).toBeInTheDocument()

    emit({ id: 'm1', status: 'READY', progress: 100 })
    expect(await screen.findByText('Pronto')).toBeInTheDocument()
    await waitFor(() => expect(onReady).toHaveBeenCalledWith(expect.objectContaining({ id: 'm1', status: 'READY' })))
    expect(fetchMock.mock.calls.some(([u]) => String(u).endsWith('/media/m1'))).toBe(true)
  })

  it('imagem acima de 10 MB é recusada antes do envio', async () => {
    mockFetch(() => jsonResponse(LIMITS))
    renderUploader()
    const user = userEvent.setup()
    const big = new File([new Uint8Array(10 * 1024 * 1024 + 1)], 'grande.png', { type: 'image/png' })
    await user.upload(screen.getByLabelText('Enviar mídia'), big)
    expect(await screen.findByText('Imagem maior que 10 MB.')).toBeInTheDocument()
    expect(uploadMock).not.toHaveBeenCalled()
  })

  it('falha de rede → erro com "tentar novamente" → reenvio conclui', async () => {
    mockFetch(() => jsonResponse(LIMITS))
    uploadMock.mockRejectedValueOnce(toApiError(0, undefined)).mockResolvedValueOnce(media({ status: 'READY', name: 'foto.jpg', kind: 'IMAGE' }))
    renderUploader()
    const user = userEvent.setup()
    await user.upload(screen.getByLabelText('Enviar mídia'), new File([new Uint8Array(10)], 'foto.jpg', { type: 'image/jpeg' }))
    expect(await screen.findByText('Não foi possível enviar o arquivo. Tente novamente.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Tentar enviar foto.jpg novamente' }))
    expect(await screen.findByText('Pronto')).toBeInTheDocument()
    expect(uploadMock).toHaveBeenCalledTimes(2)
  })

  it('arquivo recusado pelo servidor (REJECTED) mostra o motivo; falha de processamento (FAILED) reprocessa', async () => {
    const fetchMock = mockFetch((url, init) => {
      if (url.endsWith('/media/limits')) return jsonResponse(LIMITS)
      if (url.endsWith('/media/m2/retry') && init.method === 'POST') return jsonResponse(media({ id: 'm2', status: 'UPLOADED' }), 202)
      return jsonResponse({})
    })
    uploadMock
      .mockResolvedValueOnce(media({ id: 'm1', status: 'REJECTED', status_message: 'Resolução abaixo de Full HD.' }))
      .mockResolvedValueOnce(media({ id: 'm2', status: 'FAILED', status_message: 'Tempo de processamento esgotado.' }))
    renderUploader()
    const user = userEvent.setup()
    await user.upload(screen.getByLabelText('Enviar mídia'), [
      new File([new Uint8Array(10)], 'a.mp4', { type: 'video/mp4' }),
      new File([new Uint8Array(10)], 'b.mp4', { type: 'video/mp4' }),
    ])
    expect(await screen.findByText('Resolução abaixo de Full HD.')).toBeInTheDocument()
    expect(await screen.findByText(/Tempo de processamento esgotado/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Tentar enviar b.mp4 novamente' }))
    await waitFor(() => expect(fetchMock.mock.calls.some(([u, i]) => String(u).endsWith('/media/m2/retry') && (i as RequestInit).method === 'POST')).toBe(true))
    expect(uploadMock).toHaveBeenCalledTimes(2)
  })

  it('equipe RD sem organização escolhida não consegue enviar', async () => {
    mockFetch(() => jsonResponse(LIMITS))
    renderUploader({ orgId: null, requireOrg: true })
    expect(await screen.findByText('Escolha a organização dona da mídia antes de enviar arquivos.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Selecionar arquivos' })).toBeDisabled()
  })
})
