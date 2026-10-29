import { EMPTY_VALUE } from '@/lib/format'

/** Data de calendário "AAAA-MM-DD" (sem hora/fuso) → "DD/MM/AAAA", sem conversão de fuso. */
export function formatCalendarDate(value: string | null | undefined): string {
  if (!value) return EMPTY_VALUE
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(value)
  return m ? `${m[3]}/${m[2]}/${m[1]}` : value
}
