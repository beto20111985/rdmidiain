/** Conversões/validações da programação (período e horários) usadas no wizard e nas alterações. */
import type { ScheduleValue } from '@/components/campaigns/schedule-editor'
import { validateBands, validateSpecificTimes } from './rules'
import { todayIn } from '@/lib/dates'
import type { CampaignDetail } from './types'

/** Hoje no fuso padrão (America/Sao_Paulo), "YYYY-MM-DD". */
export function todayIso(): string {
  return todayIn()
}

export function scheduleFrom(c: CampaignDetail): ScheduleValue {
  return {
    period_type: c.period_type,
    period_days: null,
    start_date: c.start_date ?? '',
    end_date: c.end_date ?? '',
    days_mask: c.days_mask,
    time_bands: c.time_bands?.length ? c.time_bands : [[0, 1440]],
    freq_kind: c.freq_kind,
    freq_value: c.freq_value,
    specific_times: c.specific_times ?? [],
    min_interval_s: c.min_interval_s,
    priority: c.priority,
    exclusivity: c.exclusivity,
    allow_same_advertiser_consecutive: c.allow_same_advertiser_consecutive,
    auto_renew: c.auto_renew ?? false,
  }
}

/** Erros de cliente para a programação (objeto vazio = ok). */
export function validateSchedule(v: ScheduleValue, { requireDates = true } = {}): Record<string, string> {
  const e: Record<string, string> = {}
  if (requireDates) {
    if (!v.start_date) e.start_date = 'Informe a data de início.'
    else if (v.start_date < todayIso()) e.start_date = 'A data de início já passou.'
    if (v.period_type === 'AVULSO') {
      if (!v.end_date) e.end_date = 'Informe a data final.'
      else if (v.start_date && v.end_date < v.start_date) e.end_date = 'A data final é anterior à inicial.'
    }
  }
  if (!v.days_mask) e.days_mask = 'Selecione ao menos um dia.'
  const b = validateBands(v.time_bands)
  if (b) e.time_bands = b
  if (v.freq_kind === 'SPECIFIC_TIME') {
    const s = validateSpecificTimes(v.specific_times, v.time_bands)
    if (s) e.specific_times = s
  } else if (!(v.freq_value >= 1 && v.freq_value <= 1440)) e.freq_value = 'Informe de 1 a 1440 inserções.'
  return e
}

/** Corpo de PATCH/alteração a partir da programação (o servidor calcula o fim de planos recorrentes). */
export function scheduleBody(v: ScheduleValue): Record<string, unknown> {
  const recurring = v.period_type !== 'AVULSO'
  return {
    period_type: v.period_type,
    start_date: v.start_date,
    ...(recurring ? {} : { end_date: v.end_date }),
    days_mask: v.days_mask,
    time_bands: v.time_bands,
    freq_kind: v.freq_kind,
    freq_value: v.freq_kind === 'SPECIFIC_TIME' ? v.specific_times.length : v.freq_value,
    specific_times: v.freq_kind === 'SPECIFIC_TIME' ? v.specific_times : [],
    min_interval_s: v.min_interval_s,
    priority: v.priority,
    exclusivity: v.exclusivity,
    allow_same_advertiser_consecutive: v.allow_same_advertiser_consecutive,
  }
}
