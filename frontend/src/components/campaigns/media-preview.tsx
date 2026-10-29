/**
 * Pré-visualização protegida: o arquivo é buscado com Bearer (fetch) e exibido por blob URL
 * (W-MEDIA não aceita token em URL). Vídeo sempre mudo e sem reprodução automática com som.
 */
import * as React from 'react'
import { useQuery } from '@tanstack/react-query'
import { FileVideo, ImageOff } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { qk } from '@/lib/queryKeys'
import { cn } from '@/lib/utils'
import { fetchProtectedBlob } from '@/pages/campaigns/lib/api'
import type { MediaFile } from '@/pages/campaigns/lib/types'
import { describeError } from '@/pages/campaigns/lib/errorText'

function useObjectUrl(blob: Blob | undefined): string | null {
  const [url, setUrl] = React.useState<string | null>(null)
  React.useEffect(() => {
    if (!blob || typeof URL.createObjectURL !== 'function') {
      setUrl(null)
      return
    }
    const u = URL.createObjectURL(blob)
    setUrl(u)
    return () => URL.revokeObjectURL(u)
  }, [blob])
  return url
}

export function MediaPreview({
  media,
  variant = 'preview',
  className,
  thumbnail = false,
}: {
  media: Pick<MediaFile, 'id' | 'name' | 'kind' | 'status' | 'has_preview'>
  variant?: 'preview' | 'content'
  className?: string
  /** Miniatura: imagens apenas (vídeo mostra ícone até abrir o detalhe). */
  thumbnail?: boolean
}) {
  const enabled = media.status === 'READY' && (variant === 'content' || media.has_preview) && !(thumbnail && media.kind === 'VIDEO')
  const q = useQuery({
    queryKey: qk.sub('media', 'detail', media.id, 'blob', variant),
    queryFn: ({ signal }) => fetchProtectedBlob(`/media/${media.id}/${variant}`, signal),
    enabled,
    staleTime: 30 * 60 * 1000,
    gcTime: 5 * 60 * 1000,
  })
  const url = useObjectUrl(q.data)
  const box = cn('flex items-center justify-center overflow-hidden rounded-lg bg-field', className)

  if (thumbnail && media.kind === 'VIDEO') {
    return (
      <div className={box} aria-hidden="true">
        <FileVideo className="size-6 text-text-muted" />
      </div>
    )
  }
  if (!enabled) {
    return (
      <div className={box}>
        <ImageOff aria-hidden="true" className="size-6 text-text-muted" />
        <span className="sr-only">Pré-visualização indisponível</span>
      </div>
    )
  }
  if (q.isLoading || (!url && !q.error)) return <Skeleton className={cn('rounded-lg', className)} />
  if (q.error)
    return (
      <div className={box} role="img" aria-label={`Pré-visualização indisponível: ${describeError(q.error)}`}>
        <ImageOff aria-hidden="true" className="size-6 text-text-muted" />
      </div>
    )
  if (media.kind === 'VIDEO') {
    return (
      <video
        className={cn('rounded-lg bg-black', className)}
        src={url ?? undefined}
        controls
        muted
        playsInline
        preload="metadata"
        aria-label={`Pré-visualização de ${media.name} (sem áudio)`}
      />
    )
  }
  return <img className={cn('rounded-lg object-contain', className)} src={url ?? undefined} alt={`Pré-visualização de ${media.name}`} />
}
