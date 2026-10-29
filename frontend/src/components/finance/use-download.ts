import { useCallback, useState } from 'react'
import { toast } from 'sonner'
import { errorMessage } from '@/lib/errors'
import { downloadFile, type DownloadOptions, type DownloadResult } from './download'

/**
 * Estado de download por chave (para o botão certo mostrar "Baixando…" e ficar desabilitado).
 * Sucesso → toast curto; erro → toast com a mensagem do servidor (o botão volta ao normal).
 */
export function useDownload() {
  const [pending, setPending] = useState<string | null>(null)

  const run = useCallback(async (key: string, path: string, opts: DownloadOptions): Promise<DownloadResult | null> => {
    setPending(key)
    try {
      const r = await downloadFile(path, opts)
      toast.success(`Download iniciado: ${r.filename}`)
      return r
    } catch (e) {
      toast.error(errorMessage(e))
      return null
    } finally {
      setPending(null)
    }
  }, [])

  return { pending, run, isPending: (key: string) => pending === key }
}
