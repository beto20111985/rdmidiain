/**
 * Período dos painéis e relatórios (DS §10 "Seletores de período: Hoje, 7 dias, 30 dias,
 * Personalizado"). Datas em `YYYY-MM-DD` no fuso exibido (padrão America/Sao_Paulo) —
 * nunca o fuso do navegador. O estado fica na URL (`periodo`, `inicio`, `fim`) para ser
 * preservado ao navegar/voltar e em caso de erro de rede.
 */
import { useCallback, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import { DEFAULT_TIMEZONE, EMPTY_VALUE } from '@/lib/format'
import { todayIn } from '@/lib/dates'

export type PeriodPreset = 'hoje' | '7' | '30' | 'custom'

export interface Period {
  preset: PeriodPreset
  /** YYYY-MM-DD (inclusive) */
  start: string
  /** YYYY-MM-DD (inclusive) */
  end: string
}

export const PERIOD_PRESETS: Array<{ value: PeriodPreset; label: string }> = [
  { value: 'hoje', label: 'Hoje' },
  { value: '7', label: '7 dias' },
  { value: '30', label: '30 dias' },
  { value: 'custom', label: 'Personalizado' },
]

/** Período máximo aceito pelos relatórios do backend (MAX_PERIOD_DAYS). */
export const MAX_PERIOD_DAYS = 366

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/

export function isIsoDate(v: string | null | undefined): v is string {
  if (!v || !ISO_DATE.test(v)) return false
  const d = new Date(`${v}T00:00:00Z`)
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === v
}


/** Soma dias a uma data YYYY-MM-DD (aritmética de calendário, sem fuso). */
export function shiftDate(iso: string, days: number): string {
  const d = new Date(`${iso}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() + days)
  return d.toISOString().slice(0, 10)
}

/** Dias corridos entre duas datas, inclusive. */
export function daysBetween(start: string, end: string): number {
  const a = Date.UTC(+start.slice(0, 4), +start.slice(5, 7) - 1, +start.slice(8, 10))
  const b = Date.UTC(+end.slice(0, 4), +end.slice(5, 7) - 1, +end.slice(8, 10))
  return Math.round((b - a) / 86_400_000) + 1
}

export function presetRange(preset: Exclude<PeriodPreset, 'custom'>, timeZone = DEFAULT_TIMEZONE, now = new Date()) {
  const end = todayIn(timeZone, now)
  const days = preset === 'hoje' ? 1 : Number(preset)
  return { start: shiftDate(end, -(days - 1)), end }
}

/** "dd/mm/aaaa" a partir de YYYY-MM-DD (sem conversão de fuso: a data já é local). */
export function formatIsoDate(iso: string | null | undefined): string {
  if (!iso) return EMPTY_VALUE
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso)
  return m ? `${m[3]}/${m[2]}/${m[1]}` : EMPTY_VALUE
}

/** "dd/mm" curto para eixos de gráfico. */
export function formatIsoDayMonth(iso: string | number): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso))
  return m ? `${m[3]}/${m[2]}` : String(iso)
}

/** "mm/aaaa" para competências (primeiro dia do mês). */
export function formatIsoMonth(iso: string | null | undefined): string {
  if (!iso) return EMPTY_VALUE
  const m = /^(\d{4})-(\d{2})/.exec(iso)
  return m ? `${m[2]}/${m[1]}` : EMPTY_VALUE
}

export function periodLabel(p: Period): string {
  if (p.preset === 'hoje') return `Hoje (${formatIsoDate(p.end)})`
  if (p.preset === '7') return `Últimos 7 dias (${formatIsoDate(p.start)} a ${formatIsoDate(p.end)})`
  if (p.preset === '30') return `Últimos 30 dias (${formatIsoDate(p.start)} a ${formatIsoDate(p.end)})`
  return p.start === p.end ? formatIsoDate(p.start) : `${formatIsoDate(p.start)} a ${formatIsoDate(p.end)}`
}

/** Valida um período personalizado; devolve a mensagem de erro ou null. */
export function validatePeriod(start: string, end: string): string | null {
  if (!isIsoDate(start) || !isIsoDate(end)) return 'Informe as datas inicial e final.'
  if (end < start) return 'A data final não pode ser anterior à inicial.'
  if (daysBetween(start, end) > MAX_PERIOD_DAYS) return `O período máximo é de ${MAX_PERIOD_DAYS} dias.`
  return null
}

export interface UsePeriodOptions {
  defaultPreset?: Exclude<PeriodPreset, 'custom'>
  timeZone?: string
  /** Prefixo dos parâmetros na URL (mais de um seletor na mesma página). */
  prefix?: string
}

/** Período guardado na URL; período personalizado inválido cai no padrão. */
export function usePeriodState({ defaultPreset = '30', timeZone = DEFAULT_TIMEZONE, prefix = '' }: UsePeriodOptions = {}) {
  const [sp, setSp] = useSearchParams()
  const key = useCallback((n: string) => `${prefix}${n}`, [prefix])
  const rawPreset = sp.get(key('periodo'))
  const rawStart = sp.get(key('inicio'))
  const rawEnd = sp.get(key('fim'))

  const period: Period = useMemo(() => {
    if (rawPreset === 'custom' && rawStart && rawEnd && validatePeriod(rawStart, rawEnd) === null) {
      return { preset: 'custom', start: rawStart, end: rawEnd }
    }
    const preset = rawPreset === 'hoje' || rawPreset === '7' || rawPreset === '30' ? rawPreset : defaultPreset
    return { preset, ...presetRange(preset, timeZone) }
  }, [rawPreset, rawStart, rawEnd, defaultPreset, timeZone])

  const setPeriod = useCallback(
    (p: Period) => {
      setSp(
        (prev) => {
          const next = new URLSearchParams(prev)
          if (p.preset === defaultPreset) next.delete(key('periodo'))
          else next.set(key('periodo'), p.preset)
          if (p.preset === 'custom') {
            next.set(key('inicio'), p.start)
            next.set(key('fim'), p.end)
          } else {
            next.delete(key('inicio'))
            next.delete(key('fim'))
          }
          return next
        },
        { replace: true },
      )
    },
    [setSp, key, defaultPreset],
  )

  return { period, setPeriod, timeZone }
}

/**
 * Série diária completa do período: dias sem linha no relatório = 0 exibições válidas registradas
 * (o relatório só devolve dias com proof-of-play). Evita que o gráfico ligue dias distantes.
 */
export function fillDailySeries(rows: Array<{ day: string; valid: number | null }>, start: string, end: string): Array<{ day: string; valid: number }> {
  const byDay = new Map(rows.map((r) => [r.day.slice(0, 10), r.valid ?? 0]))
  const out: Array<{ day: string; valid: number }> = []
  const total = daysBetween(start, end)
  if (total <= 0 || total > MAX_PERIOD_DAYS) return rows.map((r) => ({ day: r.day, valid: r.valid ?? 0 }))
  for (let i = 0; i < total; i++) {
    const d = shiftDate(start, i)
    out.push({ day: d, valid: byDay.get(d) ?? 0 })
  }
  return out
}
