/**
 * Datas e horas digitadas pelo usuário (hora de parede) ↔ instante UTC no FUSO IANA correto
 * (contrato v1 §1). Utilitário ÚNICO da plataforma — consolidado de `pages/campaigns/lib/dates.ts`
 * (FE-CAMP), `components/ops/utils.ts` (FE-OPS) e `pages/admin/audit/dates.ts` (FE-ADM).
 *
 * Regras: America/Sao_Paulo (DEFAULT_TIMEZONE da fundação) para dado sem estabelecimento; o fuso
 * IANA do estabelecimento quando o dado é dele. Nunca usa o fuso do navegador nem offset fixo
 * (Manaus −04, Rio Branco −05, Noronha −02, horário de verão histórico de São Paulo até 2019).
 * Fuso inválido/ausente cai no padrão; entrada inválida → `null`.
 */
import { DEFAULT_TIMEZONE } from '@/lib/format'

/** Deslocamento (ms) do fuso `tz` no instante `utcMs` (positivo a leste de UTC). */
function offsetMs(utcMs: number, tz: string): number {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: tz,
    hourCycle: 'h23',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).formatToParts(new Date(utcMs))
  const get = (t: string) => Number(parts.find((p) => p.type === t)?.value)
  return Date.UTC(get('year'), get('month') - 1, get('day'), get('hour'), get('minute'), get('second')) - utcMs
}

/** Fuso válido? (IANA reconhecido pelo Intl) */
export function isTimeZone(tz: string | null | undefined): tz is string {
  if (!tz) return false
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: tz })
    return true
  } catch {
    return false
  }
}

const zoneOrDefault = (tz: string | null | undefined): string => (isTimeZone(tz) ? tz : DEFAULT_TIMEZONE)

/**
 * Hora de parede `date` ("YYYY-MM-DD") + `time` ("HH:mm" ou "HH:mm:ss") no fuso `tz` → ISO-8601 UTC com `Z`.
 * Ex.: ('2026-10-07', '23:59:59', 'America/Manaus') → '2026-10-08T03:59:59Z'.
 */
export function zonedToUtcIso(date: string, time: string, tz: string | null | undefined = DEFAULT_TIMEZONE): string | null {
  const d = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date || '')
  const t = /^(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(time || '')
  if (!d || !t) return null
  const zone = zoneOrDefault(tz)
  const wall = Date.UTC(Number(d[1]), Number(d[2]) - 1, Number(d[3]), Number(t[1]), Number(t[2]), Number(t[3] ?? 0))
  if (Number.isNaN(wall)) return null
  let utc = wall - offsetMs(wall, zone)
  utc = wall - offsetMs(utc, zone) // segunda passada: transições de horário de verão
  return new Date(utc).toISOString().replace('.000Z', 'Z')
}

/** Valor de `<input type="datetime-local">` ("2026-10-07T14:30") no fuso `tz` → ISO UTC com `Z`. */
export function zonedLocalToUtcIso(local: string, tz: string | null | undefined = DEFAULT_TIMEZONE): string | null {
  const m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}(?::\d{2})?)$/.exec(local || '')
  return m ? zonedToUtcIso(m[1], m[2], tz) : null
}

/** Instante → valor de `<input type="datetime-local">` (hora de parede no fuso `tz`, "YYYY-MM-DDTHH:mm"). */
export function utcToZonedLocalInput(value: string | number | Date, tz: string | null | undefined = DEFAULT_TIMEZONE): string {
  const ms = new Date(value).getTime()
  return new Date(ms + offsetMs(ms, zoneOrDefault(tz))).toISOString().slice(0, 16)
}

/** Início do dia local (00:00:00) no fuso. */
export function startOfDayUtc(date: string, tz: string | null | undefined = DEFAULT_TIMEZONE): string | null {
  return zonedToUtcIso(date, '00:00:00', tz)
}

/** Fim do dia local (23:59:59) no fuso — mesma regra de vigência das campanhas. */
export function endOfDayUtc(date: string, tz: string | null | undefined = DEFAULT_TIMEZONE): string | null {
  return zonedToUtcIso(date, '23:59:59', tz)
}

/** Início do dia SEGUINTE no fuso (limite exclusivo de filtros "até o dia X"). */
export function nextDayStartUtc(date: string, tz: string | null | undefined = DEFAULT_TIMEZONE): string | null {
  const d = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date || '')
  if (!d) return null
  const next = new Date(Date.UTC(Number(d[1]), Number(d[2]) - 1, Number(d[3]) + 1))
  return startOfDayUtc(next.toISOString().slice(0, 10), tz)
}

/** Instante → data local "YYYY-MM-DD" no fuso (para preencher `<input type="date">`). */
export function utcToZonedDate(value: string | number | Date, tz: string | null | undefined = DEFAULT_TIMEZONE): string {
  return new Intl.DateTimeFormat('en-CA', { timeZone: zoneOrDefault(tz), year: 'numeric', month: '2-digit', day: '2-digit' }).format(
    new Date(value),
  )
}

/** Hoje (data local) no fuso. */
export function todayIn(tz: string | null | undefined = DEFAULT_TIMEZONE, now: Date = new Date()): string {
  return utcToZonedDate(now, tz)
}

/** Rótulo curto do fuso para o usuário saber em que hora a data vale. */
export function timeZoneLabel(tz: string | null | undefined): string {
  if (!tz || tz === DEFAULT_TIMEZONE) return 'horário de Brasília (America/Sao_Paulo)'
  return `fuso do estabelecimento (${tz})`
}
