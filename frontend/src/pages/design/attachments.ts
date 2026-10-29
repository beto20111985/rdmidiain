/**
 * Anexos do briefing (backend designer/files.py): PNG, JPG, GIF, WEBP, SVG, PDF, MP4, WEBM
 * e TXT, com limite por tipo (imagem 10 MB, documento 20 MB, vídeo 200 MB, texto 1 MB).
 * Validação ANTES do envio; o servidor confere a assinatura real do arquivo (415 se falsa).
 */
import { formatBytes } from '@/lib/format'

const MIB = 1024 * 1024

export const ATTACHMENT_ACCEPT = [
  'image/png',
  'image/jpeg',
  'image/gif',
  'image/webp',
  'image/svg+xml',
  'application/pdf',
  'video/mp4',
  'video/webm',
  'text/plain',
]

export const ATTACHMENT_MAX = 200 * MIB

export function attachmentLimit(type: string): number {
  if (type.startsWith('video/')) return 200 * MIB
  if (type === 'application/pdf') return 20 * MIB
  if (type === 'text/plain') return 1 * MIB
  return 10 * MIB
}

export function attachmentProblem(file: File): string | null {
  if (!ATTACHMENT_ACCEPT.includes(file.type)) return `Formato não aceito (${file.type || 'desconhecido'}).`
  const max = attachmentLimit(file.type)
  return file.size > max ? `Arquivo maior que o limite de ${formatBytes(max)} para este tipo.` : null
}

/** Tipo de anexo do briefing inferido pelo arquivo (o usuário pode marcar como logotipo). */
export function inferAttachmentKind(file: File, preferLogo = false): 'LOGOTIPO' | 'IMAGEM' | 'VIDEO' | 'REFERENCIA' | 'TEXTO' {
  if (file.type.startsWith('image/')) return preferLogo ? 'LOGOTIPO' : 'IMAGEM'
  if (file.type.startsWith('video/')) return 'VIDEO'
  if (file.type === 'text/plain') return 'TEXTO'
  return 'REFERENCIA'
}

/** Arte entregue pelo designer: PNG/JPG em 1920×1080 ou 1080×1920; vídeo MP4. */
export const VERSION_ACCEPT_IMAGE = ['image/png', 'image/jpeg']
export const VERSION_ACCEPT_VIDEO = ['video/mp4']

export function expectedArtSize(orientation: 'HORIZONTAL' | 'VERTICAL'): { width: number; height: number } {
  return orientation === 'VERTICAL' ? { width: 1080, height: 1920 } : { width: 1920, height: 1080 }
}

/** Lê as dimensões de uma imagem local (antes do envio). */
export function readImageSize(file: File): Promise<{ width: number; height: number } | null> {
  return new Promise((resolve) => {
    if (typeof URL.createObjectURL !== 'function') return resolve(null)
    const url = URL.createObjectURL(file)
    const img = new Image()
    img.onload = () => {
      resolve({ width: img.naturalWidth, height: img.naturalHeight })
      URL.revokeObjectURL(url)
    }
    img.onerror = () => {
      resolve(null)
      URL.revokeObjectURL(url)
    }
    img.src = url
  })
}
