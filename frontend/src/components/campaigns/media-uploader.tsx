/**
 * Envio de mídia (DS §10 Upload): arrastar e soltar + botão acessível, validação prévia
 * por tipo/tamanho (imagem ≤ 10 MB, vídeo ≤ 200 MB — limites do servidor quando disponíveis),
 * PROGRESSO REAL de envio (XHR), estados selecionado → enviando → processando → pronto/erro,
 * "processando" atualizado pelo evento WS `media.processing` (sem polling) e tentar novamente.
 */
import * as React from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'
import { FileDropzone, type UploadItem } from '@/components/patterns/file-dropzone'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { uploadWithProgress } from '@/lib/upload'
import { isApiError } from '@/lib/errors'
import { formatBytes } from '@/lib/format'
import { qk } from '@/lib/queryKeys'
import { useRealtimeEvent, useRealtimeStatus } from '@/lib/realtime/context'
import { mediaApi } from '@/pages/campaigns/lib/api'
import {
  DEFAULT_IMAGE_MAX,
  DEFAULT_VIDEO_MAX,
  IMAGE_MIMES,
  VIDEO_MIMES,
  mediaKindOf,
  resolutionAllowed,
  validateMediaFile,
} from '@/pages/campaigns/lib/rules'
import { msToClock } from '@/pages/campaigns/lib/format'
import type { MediaFile, MediaLimits, MediaProcessingEvent, MediaStatus } from '@/pages/campaigns/lib/types'
import { describeError } from '@/pages/campaigns/lib/errorText'

interface Item extends UploadItem {
  mediaId?: string
  serverStatus?: MediaStatus
  clientMeta?: string
  processingPct?: number
}

export interface MediaUploaderProps {
  /** Organização dona da mídia (obrigatória para a equipe RD com escopo global). */
  orgId?: string | null
  requireOrg?: boolean
  onReady?: (media: MediaFile) => void
  disabled?: boolean
  label?: string
}

let seq = 0
const nextId = () => `up-${Date.now().toString(36)}-${++seq}`

function processingMeta(it: Item): string | undefined {
  const parts = [it.clientMeta]
  if (it.status === 'processing') {
    const pct = it.processingPct
    parts.push(pct !== undefined && pct > 0 ? `Processando no servidor: ${pct}%` : 'Na fila de processamento do servidor')
  }
  return parts.filter(Boolean).join(' · ') || undefined
}

/** Lê dimensões/duração no navegador apenas para informar o usuário (o servidor mede de novo). */
async function readClientMeta(file: File): Promise<string | undefined> {
  if (typeof URL.createObjectURL !== 'function') return undefined
  const url = URL.createObjectURL(file)
  try {
    if (mediaKindOf(file) === 'IMAGE') {
      const dims = await new Promise<{ w: number; h: number }>((resolve, reject) => {
        const img = new Image()
        img.onload = () => resolve({ w: img.naturalWidth, h: img.naturalHeight })
        img.onerror = () => reject(new Error('img'))
        img.src = url
      })
      const warn = resolutionAllowed(dims.w, dims.h) ? '' : ' · resolução fora do padrão (o servidor pode recusar)'
      return `${dims.w} × ${dims.h}${warn}`
    }
    const v = await new Promise<{ w: number; h: number; d: number }>((resolve, reject) => {
      const el = document.createElement('video')
      el.preload = 'metadata'
      el.muted = true
      el.onloadedmetadata = () => resolve({ w: el.videoWidth, h: el.videoHeight, d: el.duration })
      el.onerror = () => reject(new Error('video'))
      el.src = url
    })
    return `${v.w} × ${v.h} · ${Number.isFinite(v.d) ? msToClock(v.d * 1000) : 'duração desconhecida'}`
  } catch {
    return undefined
  } finally {
    URL.revokeObjectURL(url)
  }
}

export function MediaUploader({ orgId, requireOrg = false, onReady, disabled, label = 'Enviar mídia' }: MediaUploaderProps) {
  const qc = useQueryClient()
  const { live } = useRealtimeStatus()
  const [items, setItems] = React.useState<Item[]>([])
  const itemsRef = React.useRef<Item[]>([])
  itemsRef.current = items
  const chain = React.useRef<Promise<void>>(Promise.resolve())
  const controllers = React.useRef(new Map<string, AbortController>())
  const onReadyRef = React.useRef(onReady)
  onReadyRef.current = onReady

  // Fora do domínio 'media': os eventos media.processing não devem refazer a leitura dos limites.
  const limitsQ = useQuery({ queryKey: qk.sub('settings', 'media-limits'), queryFn: mediaApi.limits, staleTime: 60 * 60 * 1000 })
  const limits = limitsQ.data as MediaLimits | undefined
  const imageMax = limits?.image.max_bytes ?? DEFAULT_IMAGE_MAX
  const videoMax = limits?.video.max_bytes ?? DEFAULT_VIDEO_MAX
  const blockedByOrg = requireOrg && !orgId

  React.useEffect(() => {
    const map = controllers.current
    return () => map.forEach((c) => c.abort())
  }, [])

  const patch = React.useCallback((id: string, p: Partial<Item>) => {
    setItems((prev) => prev.map((it) => (it.id === id ? { ...it, ...p } : it)))
  }, [])

  const applyServer = React.useCallback(
    (id: string, m: Pick<MediaFile, 'id' | 'status' | 'status_message' | 'progress_pct'> & Partial<MediaFile>) => {
      const st = m.status
      if (st === 'READY') {
        patch(id, { mediaId: m.id, serverStatus: st, status: 'ready', error: undefined })
        if (onReadyRef.current) {
          if (m.org_id && m.source) onReadyRef.current(m as MediaFile)
          else void mediaApi.get(m.id).then((full) => onReadyRef.current?.(full)).catch(() => undefined)
        }
      } else if (st === 'REJECTED') {
        patch(id, { mediaId: m.id, serverStatus: st, status: 'error', error: m.status_message ?? 'Arquivo recusado pelo servidor.' })
      } else if (st === 'FAILED') {
        patch(id, {
          mediaId: m.id,
          serverStatus: st,
          status: 'error',
          error: `${m.status_message ?? 'Falha no processamento.'} Use "tentar novamente" para reprocessar.`,
        })
      } else {
        patch(id, { mediaId: m.id, serverStatus: st, status: 'processing', processingPct: m.progress_pct ?? 0 })
      }
    },
    [patch],
  )

  const upload = React.useCallback(
    async (id: string) => {
      const it = itemsRef.current.find((x) => x.id === id)
      if (!it) return
      const ctrl = new AbortController()
      controllers.current.set(id, ctrl)
      patch(id, { status: 'uploading', progress: 0, error: undefined })
      const form = new FormData()
      if (orgId) form.append('org_id', orgId) // a organização vai ANTES do arquivo (W-MEDIA)
      form.append('name', it.file.name)
      form.append('file', it.file)
      try {
        const res = await uploadWithProgress<MediaFile>({
          path: '/media/upload',
          form,
          signal: ctrl.signal,
          onProgress: (f) => patch(id, { progress: f }),
        })
        applyServer(id, res)
        void qc.invalidateQueries({ queryKey: qk.root('media') })
      } catch (e) {
        if ((e as { name?: string })?.name === 'AbortError') return
        const msg = isApiError(e) && e.isNetwork ? 'Não foi possível enviar o arquivo. Tente novamente.' : describeError(e)
        patch(id, { status: 'error', error: msg })
      } finally {
        controllers.current.delete(id)
      }
    },
    [orgId, patch, applyServer, qc],
  )

  const enqueue = React.useCallback(
    (id: string) => {
      chain.current = chain.current.then(() => upload(id))
    },
    [upload],
  )

  const onFiles = (files: File[]) => {
    const created: Item[] = files.map((file) => {
      const problem = validateMediaFile(file, { image: imageMax, video: videoMax })
      return problem ? { id: nextId(), file, status: 'error', error: problem } : { id: nextId(), file, status: 'selected' }
    })
    setItems((prev) => [...prev, ...created])
    itemsRef.current = [...itemsRef.current, ...created]
    for (const it of created) {
      void readClientMeta(it.file).then((meta) => {
        if (meta) patch(it.id, { clientMeta: meta })
      })
      if (it.status === 'selected') enqueue(it.id)
    }
  }

  const onRetry = async (id: string) => {
    const it = itemsRef.current.find((x) => x.id === id)
    if (!it) return
    if (it.mediaId && it.serverStatus === 'FAILED') {
      try {
        const m = await mediaApi.retry(it.mediaId)
        applyServer(id, m)
      } catch (e) {
        patch(id, { error: describeError(e) })
      }
      return
    }
    if (it.serverStatus === 'REJECTED' || validateMediaFile(it.file, { image: imageMax, video: videoMax })) return
    enqueue(id)
  }

  const refreshPending = async () => {
    const pending = itemsRef.current.filter((x) => x.status === 'processing' && x.mediaId)
    await Promise.all(
      pending.map(async (x) => {
        try {
          applyServer(x.id, await mediaApi.get(x.mediaId as string))
        } catch {
          /* mantém o estado atual */
        }
      }),
    )
  }

  useRealtimeEvent('media.processing', (ev) => {
    const d = ev.data as unknown as MediaProcessingEvent
    const it = itemsRef.current.find((x) => x.mediaId && x.mediaId === d.id)
    if (!it || !d.status) return
    applyServer(it.id, { id: d.id, status: d.status, status_message: d.message ?? null, progress_pct: d.progress ?? 0 })
  })

  const shown = items.map((it) => ({ ...it, meta: processingMeta(it) }))
  const hasProcessing = items.some((x) => x.status === 'processing')

  return (
    <div className="flex flex-col gap-3">
      {blockedByOrg ? (
        <Alert variant="warning">
          <AlertDescription>Escolha a organização dona da mídia antes de enviar arquivos.</AlertDescription>
        </Alert>
      ) : null}
      <FileDropzone
        accept={[...IMAGE_MIMES, ...VIDEO_MIMES]}
        maxSizeBytes={Math.max(imageMax, videoMax)}
        label={label}
        disabled={disabled || blockedByOrg}
        items={shown}
        onFilesAccepted={onFiles}
        onRetry={(id) => void onRetry(id)}
        onRemove={(id) => setItems((prev) => prev.filter((x) => x.id !== id))}
        requirements={
          <ul className="m-0 flex list-none flex-col gap-0.5 p-0 text-left">
            <li>Imagens: JPG, PNG ou GIF até {formatBytes(imageMax)}.</li>
            <li>Vídeos: MP4 (WebM e outros formatos são convertidos para MP4) até {formatBytes(videoMax)}.</li>
            <li>Resolução: 1920 × 1080 (horizontal), 1080 × 1920 (vertical) ou 4K (convertido para Full HD).</li>
            <li>O áudio é removido automaticamente. A duração do vídeo deve caber no anúncio (30, 60 ou 120 s).</li>
          </ul>
        }
      />
      {hasProcessing && !live ? (
        <div className="flex flex-wrap items-center gap-3 text-sm text-text-muted" role="status">
          <span>Tempo real indisponível: o status do processamento não está sendo atualizado automaticamente.</span>
          <Button variant="secondary" onClick={() => void refreshPending()}>
            <RefreshCw aria-hidden="true" />
            Verificar status agora
          </Button>
        </div>
      ) : null}
    </div>
  )
}
