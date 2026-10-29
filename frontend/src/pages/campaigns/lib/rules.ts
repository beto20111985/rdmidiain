/**
 * Regras de negócio replicadas no cliente SOMENTE para orientar o usuário antes do envio.
 * A autoridade é sempre o servidor (que revalida tudo); preço NUNCA é calculado aqui.
 */
import { z } from 'zod'
import { isApiError } from '@/lib/errors'

// ─── Duração do anúncio (P001: unidade de 30 s; 15/45/90 proibidos) ──────────
export const ALLOWED_DURATIONS = [30, 60, 120] as const
export type AllowedDuration = (typeof ALLOWED_DURATIONS)[number]
export const DURATION_MESSAGE = 'Duração permitida: 30, 60 ou 120 segundos.'

export function isAllowedDuration(v: unknown): v is AllowedDuration {
  return typeof v === 'number' && (ALLOWED_DURATIONS as readonly number[]).includes(v)
}

export const durationSchema = z.coerce
  .number({ invalid_type_error: DURATION_MESSAGE })
  .refine((v) => isAllowedDuration(v), { message: DURATION_MESSAGE })

/** Tolerância do backend (creatives.duration_problem): −1000 ms / +40 ms; nunca cortamos a mídia. */
export function videoFitsSlot(mediaDurationMs: number | null | undefined, slotS: number): boolean | null {
  if (mediaDurationMs === null || mediaDurationMs === undefined) return null
  return mediaDurationMs >= slotS * 1000 - 1000 && mediaDurationMs <= slotS * 1000 + 40
}

// ─── Dias da semana (bit 0 = segunda … bit 6 = domingo) ──────────────────────
export function daysFromMask(mask: number): number[] {
  return [0, 1, 2, 3, 4, 5, 6].filter((b) => (mask & (1 << b)) !== 0)
}
export function maskFromDays(days: number[]): number {
  return days.reduce((m, b) => m | (1 << b), 0)
}

// ─── Faixas horárias (minutos locais, alinhadas à hora) ──────────────────────
export function hourLabel(min: number): string {
  return `${String(Math.floor(min / 60)).padStart(2, '0')}:${String(min % 60).padStart(2, '0')}`
}

export function validateBands(bands: number[][]): string | null {
  if (!bands.length) return 'Informe ao menos uma faixa horária.'
  for (const b of bands) {
    if (b.length !== 2) return 'Faixa horária inválida.'
    const [s, e] = b
    if (!(s >= 0 && s < e && e <= 1440) || s % 60 !== 0 || e % 60 !== 0)
      return 'Faixas horárias devem estar alinhadas à hora (00:00–24:00) e terminar depois do início.'
  }
  const sorted = [...bands].sort((a, b) => a[0] - b[0])
  for (let i = 1; i < sorted.length; i++) if (sorted[i][0] < sorted[i - 1][1]) return 'Faixas horárias sobrepostas.'
  return null
}

const HHMM = /^([01]\d|2[0-3]):([0-5]\d)$/
export function parseHHMM(v: string): number | null {
  const m = HHMM.exec(v.trim())
  return m ? Number(m[1]) * 60 + Number(m[2]) : null
}

export function validateSpecificTimes(times: string[], bands: number[][]): string | null {
  if (!times.length) return 'Informe ao menos um horário específico.'
  if (new Set(times).size !== times.length) return 'Horários específicos repetidos.'
  for (const t of times) {
    const m = parseHHMM(t)
    if (m === null) return `Horário inválido: ${t} (use HH:MM).`
    if (!bands.some(([s, e]) => s <= m && m < e)) return `O horário ${t} está fora das faixas horárias escolhidas.`
  }
  return null
}

// ─── Política de rotação (W-INV): I%30, E%30, (I+E) divide 3600 ──────────────
export function validateRotation(internalS: number, externalS: number): string | null {
  if (!Number.isInteger(internalS) || !Number.isInteger(externalS) || internalS <= 0 || externalS <= 0)
    return 'Informe tempos inteiros positivos.'
  if (internalS % 30 !== 0) return 'O bloco interno deve ser múltiplo de 30 segundos.'
  if (externalS % 30 !== 0) return 'O bloco externo deve ser múltiplo de 30 segundos.'
  if (3600 % (internalS + externalS) !== 0) return 'O ciclo (interno + externo) deve dividir 3600 segundos (1 hora).'
  return null
}

// ─── Cidade (chave normalizada igual ao backend: "GO:goiania") ───────────────
export function slugify(text: string): string {
  return text
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
}
export function cityKey(uf: string, city: string): string {
  return `${uf.trim().toUpperCase()}:${slugify(city)}`
}
/** "GO:goiania" → "goiania (GO)" para exibição (o backend só devolve a chave). */
export function cityKeyLabel(key: string | null | undefined): string {
  if (!key) return '—'
  const [uf, name] = key.split(':')
  if (!name) return key
  return `${name.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())} (${uf})`
}

// ─── Mídia: validação prévia no cliente (o servidor revalida magic bytes etc.) ─
export const IMAGE_MIMES = ['image/jpeg', 'image/png', 'image/gif']
export const VIDEO_MIMES = [
  'video/mp4',
  'video/webm',
  'video/quicktime',
  'video/x-matroska',
  'video/x-msvideo',
  'video/avi',
  'video/mp2t',
]
export const MIB = 1024 * 1024
export const DEFAULT_IMAGE_MAX = 10 * MIB
export const DEFAULT_VIDEO_MAX = 200 * MIB

export function mediaKindOf(file: File): 'IMAGE' | 'VIDEO' | null {
  if (IMAGE_MIMES.includes(file.type)) return 'IMAGE'
  if (VIDEO_MIMES.includes(file.type)) return 'VIDEO'
  return null
}

export function validateMediaFile(file: File, limits: { image: number; video: number }): string | null {
  const kind = mediaKindOf(file)
  if (!kind) return 'Formato não aceito. Envie JPG, PNG, GIF ou vídeo (MP4, WebM).'
  if (file.size === 0) return 'Arquivo vazio.'
  if (kind === 'IMAGE' && file.size > limits.image) return `Imagem maior que ${Math.round(limits.image / MIB)} MB.`
  if (kind === 'VIDEO' && file.size > limits.video) return `Vídeo maior que ${Math.round(limits.video / MIB)} MB.`
  return null
}

export const ALLOWED_RESOLUTIONS = [
  [1920, 1080],
  [1080, 1920],
  [3840, 2160],
  [2160, 3840],
  [4096, 2160],
  [2160, 4096],
]
export function resolutionAllowed(w: number, h: number): boolean {
  return ALLOWED_RESOLUTIONS.some(([a, b]) => a === w && b === h)
}

// ─── Erros do servidor → campos do formulário ───────────────────────────────
/** Códigos de erro do backend que pertencem a um campo específico do wizard. */
export const ERROR_FIELD: Record<string, string> = {
  DURATION_NOT_ALLOWED: 'duration_s',
  DURATION_MISMATCH: 'duration_s',
  CREATIVE_DURATION_MISMATCH: 'duration_s',
  NAME_INVALID: 'name',
  PERIOD_NOT_ALLOWED: 'period_type',
  PERIOD_DAYS_REQUIRED: 'period_days',
  FREQUENCY_INVALID: 'freq_value',
  DAYS_INVALID: 'days_mask',
  MIN_INTERVAL_INVALID: 'min_interval_s',
  TIME_BAND_INVALID: 'time_bands',
  SPECIFIC_TIME_INVALID: 'specific_times',
  SPECIFIC_TIME_OUTSIDE_BAND: 'specific_times',
  DATES_INVALID: 'end_date',
  CAMPAIGN_START_IN_PAST: 'start_date',
  CAMPAIGN_INCOMPLETE: 'start_date',
  TARGET_INVALID: 'targets',
  TARGET_REQUIRED: 'targets',
  TARGET_ESTABLISHMENT_NOT_ALLOWED: 'targets',
  NO_SCREENS_AVAILABLE: 'targets',
}

/**
 * Converte um erro da API em erros por campo: 422 (`details.errors`) e 400 de negócio com
 * código conhecido. Retorna `{}` quando o erro é geral (exibir em <Alert>).
 */
export function serverFieldErrors(err: unknown): Record<string, string> {
  if (!isApiError(err)) return {}
  const out: Record<string, string> = { ...err.fieldErrors }
  const f = ERROR_FIELD[err.code]
  if (f && !out[f]) out[f] = err.message
  return out
}
