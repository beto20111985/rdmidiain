/**
 * Entrada de valores em BRL → centavos inteiros (contrato v1 §1: dinheiro sempre em
 * `*_cents` inteiros; nunca float). "1.234,56" → 123456; "10" → 1000; inválido → null.
 */
export function parseBrlToCents(raw: string): number | null {
  const s = raw.replace(/\s|R\$/g, '').trim()
  if (!s) return null
  if (!/^\d{1,3}(\.\d{3})*(,\d{1,2})?$|^\d+(,\d{1,2})?$|^\d+(\.\d{1,2})?$/.test(s)) return null
  let normalized: string
  if (s.includes(',')) normalized = s.replace(/\./g, '').replace(',', '.')
  else if (/^\d+\.\d{1,2}$/.test(s)) normalized = s
  else normalized = s.replace(/\./g, '')
  const [int, frac = ''] = normalized.split('.')
  const cents = Number(int) * 100 + Number((frac + '00').slice(0, 2))
  return Number.isSafeInteger(cents) ? cents : null
}

/** Centavos → texto editável "1234,56". */
export function centsToInput(cents: number | null | undefined): string {
  if (cents === null || cents === undefined || !Number.isFinite(cents)) return ''
  const int = Math.trunc(cents / 100)
  const frac = Math.abs(cents % 100)
  return `${int},${String(frac).padStart(2, '0')}`
}
