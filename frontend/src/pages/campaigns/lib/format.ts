/** Formatação específica do domínio de campanhas (usa os formatadores pt-BR da fundação). */
import { EMPTY_VALUE, formatDate, formatDateTime } from '@/lib/format'
import { FREQ_LABEL, PERIOD_LABEL, REGIONS, TARGET_LABEL, WEEKDAYS } from './labels'
import { cityKeyLabel, daysFromMask, hourLabel } from './rules'
import type { Campaign, PlayingState, Target } from './types'

/** Datas "YYYY-MM-DD" (sem hora) — exibidas sem conversão de fuso. */
export function formatPlainDate(d: string | null | undefined): string {
  if (!d) return EMPTY_VALUE
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(d)
  return m ? `${m[3]}/${m[2]}/${m[1]}` : formatDate(d)
}

export function periodText(c: Pick<Campaign, 'start_date' | 'end_date' | 'period_type'>): string {
  if (!c.start_date) return EMPTY_VALUE
  return `${formatPlainDate(c.start_date)} a ${formatPlainDate(c.end_date)} · ${PERIOD_LABEL[c.period_type] ?? c.period_type}`
}

export function daysText(mask: number): string {
  const days = daysFromMask(mask)
  if (days.length === 7) return 'Todos os dias'
  if (!days.length) return EMPTY_VALUE
  return days.map((b) => WEEKDAYS[b].short).join(', ')
}

export function bandsText(bands: number[][] | null | undefined): string {
  if (!bands?.length) return EMPTY_VALUE
  return bands.map(([s, e]) => `${hourLabel(s)}–${hourLabel(e)}`).join(', ')
}

export function frequencyText(c: Pick<Campaign, 'freq_kind' | 'freq_value' | 'specific_times'>): string {
  if (c.freq_kind === 'SPECIFIC_TIME') return `Horários: ${(c.specific_times ?? []).join(', ') || EMPTY_VALUE}`
  const unit = c.freq_kind === 'PER_HOUR' ? 'por hora' : 'por dia'
  return `${c.freq_value} inserção(ões) ${unit}`
}

export function freqKindText(k: string): string {
  return (FREQ_LABEL as Record<string, string>)[k] ?? k
}

export function targetText(t: Target, names?: Record<string, string>): string {
  const label = TARGET_LABEL[t.type] ?? t.type
  let value = t.value
  if (t.type === 'REGION') value = REGIONS.find((r) => r.value === t.value)?.label ?? t.value
  else if (t.type === 'COUNTRY') value = t.value === 'BRA' ? 'Brasil' : t.value
  else if (t.type === 'CITY') value = cityKeyLabel(t.value)
  else if (t.type === 'NEIGHBORHOOD') {
    const parts = t.value.split(':')
    value = parts.length >= 3 ? `${parts[2].replace(/-/g, ' ')} · ${cityKeyLabel(`${parts[0]}:${parts[1]}`)}` : t.value
  } else if ((t.type === 'CATEGORY' || t.type === 'ESTABLISHMENT') && names?.[t.value]) value = names[t.value]
  else if (t.type === 'ESTABLISHMENT') value = 'Estabelecimento selecionado pela RD'
  return `${t.exclude ? 'Excluir' : 'Incluir'} · ${label}: ${value}`
}

export function playingText(state: PlayingState | null | undefined): string {
  if (!state || state.source === 'unavailable' || state.playing === null) return 'Comprovação de exibição indisponível.'
  if (state.playing)
    return `Reproduzindo — exibição comprovada em ${formatDateTime(state.last_valid_play_at)} (janela de ${state.window_minutes ?? '—'} min).`
  return state.last_valid_play_at
    ? `Sem exibição comprovada recente. Última: ${formatDateTime(state.last_valid_play_at)}.`
    : 'Nenhuma exibição comprovada até agora.'
}

export function shortId(id: string | null | undefined): string {
  return id ? id.slice(0, 8) : EMPTY_VALUE
}

/** ms → "0:30" */
export function msToClock(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return EMPTY_VALUE
  const s = Math.round(ms / 1000)
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}
