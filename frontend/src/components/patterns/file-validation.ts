import { formatBytes } from '@/lib/format'

/** Valida tipo e tamanho ANTES do envio (DS §10 Upload). Retorna a mensagem de erro ou null. */
export function validateFile(file: File, accept: string[], maxSizeBytes: number): string | null {
  const okType = accept.some((a) => (a.endsWith('/*') ? file.type.startsWith(a.slice(0, -1)) : file.type === a))
  if (!okType) return `Formato não aceito (${file.type || 'desconhecido'}).`
  if (file.size > maxSizeBytes) return `Arquivo maior que o limite de ${formatBytes(maxSizeBytes)}.`
  return null
}
