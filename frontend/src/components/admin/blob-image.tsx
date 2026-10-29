/**
 * Imagem protegida (rota autenticada) exibida por object URL — o token nunca vai na URL.
 * A URL é revogada ao desmontar/trocar o arquivo.
 */
import { useQuery } from '@tanstack/react-query'
import { ImageOff } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { errorMessage } from '@/lib/errors'
import type { QueryDomain } from '@/lib/queryKeys'
import { cn } from '@/lib/utils'
import { fetchBinary } from './binary'
import { useObjectUrl } from './hooks'

export interface BlobImageProps {
  domain: QueryDomain
  /** Caminho da API (ex.: /design/requests/{id}/versions/{vid}/file). */
  path: string
  alt: string
  className?: string
  /** Proporção do contêiner (ex.: "16 / 9"). */
  aspect?: string
}

export function BlobImage({ domain, path, alt, className, aspect = '16 / 9' }: BlobImageProps) {
  const q = useQuery({
    queryKey: [domain, 'blob', path],
    queryFn: () => fetchBinary(path).then((r) => r.blob),
    staleTime: Infinity,
    retry: false,
  })
  const url = useObjectUrl(q.data)
  return (
    <div
      className={cn('relative w-full overflow-hidden rounded-lg border border-border bg-[rgb(var(--bg))]', className)}
      style={{ aspectRatio: aspect }}
    >
      {q.isLoading ? <Skeleton className="absolute inset-0 rounded-none" /> : null}
      {q.error ? (
        <div role="note" className="absolute inset-0 flex flex-col items-center justify-center gap-2 p-4 text-center text-xs text-text-muted">
          <ImageOff aria-hidden="true" className="size-6" />
          Pré-visualização indisponível: {errorMessage(q.error)}
        </div>
      ) : null}
      {url ? <img src={url} alt={alt} className="absolute inset-0 size-full object-contain" /> : null}
    </div>
  )
}
