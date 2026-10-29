import { errorMessage, isApiError } from '@/lib/errors'
import type { Installation } from '@/pages/ops/api/types'

/** Regras de apresentação/validação da instalação. */
export const installationTone = (s: string): 'success' | 'warning' | 'neutral' | 'info' =>
  s === 'COMPLETED' ? 'success' : s === 'IN_PROGRESS' ? 'warning' : s === 'CANCELLED' ? 'neutral' : 'info'

/** Fotos: somente JPEG/PNG até 10 MB (o backend confere o tipo real pelos bytes). */
export const PHOTO_ACCEPT = ['image/jpeg', 'image/png']
export const PHOTO_MAX_BYTES = 10 * 1024 * 1024
/** Assinatura: JPEG/PNG até 2 MB. */
export const SIGNATURE_MAX_BYTES = 2 * 1024 * 1024
/** Teste de conexão vale 30 min para concluir sem dispensa (installations.CONNECTION_TEST_MAX_AGE). */
export const CONNECTION_TEST_MAX_AGE_MS = 30 * 60 * 1000

export function connectionTestFresh(ct: { status: string; checked_at: string } | null | undefined, now = Date.now()): boolean {
  if (!ct || ct.status !== 'ONLINE' || !ct.checked_at) return false
  const t = new Date(ct.checked_at.endsWith('Z') || ct.checked_at.includes('+') ? ct.checked_at : `${ct.checked_at}Z`).getTime()
  return Number.isFinite(t) && now - t <= CONNECTION_TEST_MAX_AGE_MS
}

// --------------------------------------------------------------------------- PATCH /installations/{id}
export interface InstallationPatch {
  screen_id?: string | null
  technician_user_id?: string
  scheduled_date?: string | null
  notes?: string | null
}

/** Corpo do PATCH com SOMENTE os campos alterados (vazio = nada a salvar). */
export function installationPatchBody(
  inst: Pick<Installation, 'screen_id' | 'technician_user_id' | 'scheduled_date' | 'notes'>,
  next: { screenId: string | null; techId: string | null; date: string; notes: string },
): InstallationPatch {
  const body: InstallationPatch = {}
  if (next.screenId !== inst.screen_id) body.screen_id = next.screenId
  if (next.techId && next.techId !== inst.technician_user_id) body.technician_user_id = next.techId
  const date = next.date || null
  if (date !== (inst.scheduled_date ?? null)) body.scheduled_date = date
  const notes = next.notes.trim() || null
  if (notes !== (inst.notes ?? null)) body.notes = notes
  return body
}

export function installationPatchError(e: unknown): string {
  if (!isApiError(e)) return errorMessage(e)
  if (e.status === 404) return 'A tela escolhida não pertence a este estabelecimento (ou não existe mais).'
  if (e.code === 'SCREEN_DECOMMISSIONED') return 'Essa tela foi retirada definitivamente; escolha outra.'
  if (e.code === 'TECHNICIAN_OUT_OF_SCOPE') return 'O técnico escolhido não atende este estabelecimento.'
  if (e.code === 'TECHNICIAN_REQUIRED') return 'A instalação precisa de um técnico responsável.'
  if (e.code === 'INSTALLATION_INVALID_STATE') return 'Instalação concluída ou cancelada não pode mais ser alterada.'
  return errorMessage(e)
}

