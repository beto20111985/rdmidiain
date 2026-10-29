/**
 * Formatação pt-BR (contrato v1 §1): datas chegam em UTC ISO-8601 com `Z` e são exibidas
 * no fuso do contexto (padrão America/Sao_Paulo; dados de estabelecimento usam o fuso dele).
 * Dinheiro sempre em centavos (`*_cents`), percentuais em basis points (`*_bps`).
 */
export const DEFAULT_TIMEZONE = 'America/Sao_Paulo'
const LOCALE = 'pt-BR'

/** Sem dado → travessão, nunca zero inventado (DS §10). */
export const EMPTY_VALUE = '—'

function toDate(value: string | number | Date): Date | null {
  const d = value instanceof Date ? value : new Date(value)
  return Number.isNaN(d.getTime()) ? null : d
}

export function formatCents(cents: number | null | undefined): string {
  if (cents === null || cents === undefined || !Number.isFinite(cents)) return EMPTY_VALUE
  return new Intl.NumberFormat(LOCALE, { style: 'currency', currency: 'BRL' }).format(cents / 100)
}

export function formatBps(bps: number | null | undefined, fractionDigits = 2): string {
  if (bps === null || bps === undefined || !Number.isFinite(bps)) return EMPTY_VALUE
  return new Intl.NumberFormat(LOCALE, {
    style: 'percent',
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  }).format(bps / 10000)
}

export function formatNumber(value: number | null | undefined, options?: Intl.NumberFormatOptions): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return EMPTY_VALUE
  return new Intl.NumberFormat(LOCALE, options).format(value)
}

export function formatPercent(value: number | null | undefined, fractionDigits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return EMPTY_VALUE
  return new Intl.NumberFormat(LOCALE, {
    style: 'percent',
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  }).format(value / 100)
}

export function formatDate(value: string | number | Date | null | undefined, timeZone = DEFAULT_TIMEZONE): string {
  if (value === null || value === undefined) return EMPTY_VALUE
  const d = toDate(value)
  if (!d) return EMPTY_VALUE
  return new Intl.DateTimeFormat(LOCALE, { timeZone, day: '2-digit', month: '2-digit', year: 'numeric' }).format(d)
}

export function formatDateTime(
  value: string | number | Date | null | undefined,
  timeZone = DEFAULT_TIMEZONE,
): string {
  if (value === null || value === undefined) return EMPTY_VALUE
  const d = toDate(value)
  if (!d) return EMPTY_VALUE
  return new Intl.DateTimeFormat(LOCALE, {
    timeZone,
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(d)
}

export function formatTime(value: string | number | Date | null | undefined, timeZone = DEFAULT_TIMEZONE): string {
  if (value === null || value === undefined) return EMPTY_VALUE
  const d = toDate(value)
  if (!d) return EMPTY_VALUE
  return new Intl.DateTimeFormat(LOCALE, { timeZone, hour: '2-digit', minute: '2-digit' }).format(d)
}

/** "há 5 min", "há 2 h", "há 3 dias" — para último contato de telas. */
export function formatRelative(value: string | number | Date | null | undefined, now: Date = new Date()): string {
  if (value === null || value === undefined) return EMPTY_VALUE
  const d = toDate(value)
  if (!d) return EMPTY_VALUE
  const diffS = Math.round((d.getTime() - now.getTime()) / 1000)
  const rtf = new Intl.RelativeTimeFormat(LOCALE, { numeric: 'auto', style: 'short' })
  const abs = Math.abs(diffS)
  if (abs < 60) return rtf.format(diffS, 'second')
  if (abs < 3600) return rtf.format(Math.round(diffS / 60), 'minute')
  if (abs < 86400) return rtf.format(Math.round(diffS / 3600), 'hour')
  return rtf.format(Math.round(diffS / 86400), 'day')
}

/** Duração em segundos → "0:30", "1:05:00". */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return EMPTY_VALUE
  const s = Math.max(0, Math.round(seconds))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const r = s % 60
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}` : `${m}:${String(r).padStart(2, '0')}`
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return EMPTY_VALUE
  const units = ['B', 'KB', 'MB', 'GB']
  let v = bytes
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${formatNumber(v, { maximumFractionDigits: i === 0 ? 0 : 1 })} ${units[i]}`
}
