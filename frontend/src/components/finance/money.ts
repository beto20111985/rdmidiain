/**
 * Conversões de dinheiro digitado (pt-BR) para centavos inteiros (contrato v1 §1 — nunca float
 * no envio). "1.234,56" → 123456; "10" → 1000; "0,5" → 50. Inválido → null.
 */
export function parseBRLToCents(input: string | null | undefined): number | null {
  if (input === null || input === undefined) return null
  const s = String(input).replace(/R\$\s?/i, '').replace(/\s/g, '').trim()
  if (!s) return null
  // pt-BR: ponto = milhar, vírgula = decimal
  const m = /^(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?$/.exec(s)
  if (!m) return null
  const intPart = m[1].replace(/\./g, '')
  const frac = (m[2] ?? '').padEnd(2, '0')
  const cents = Number(intPart) * 100 + Number(frac || '0')
  return Number.isSafeInteger(cents) ? cents : null
}

/** Centavos → texto editável "1234,56" (sem símbolo, para preencher campos). */
export function centsToInput(cents: number | null | undefined): string {
  if (cents === null || cents === undefined || !Number.isFinite(cents)) return ''
  const neg = cents < 0
  const abs = Math.abs(Math.trunc(cents))
  const s = `${Math.floor(abs / 100)},${String(abs % 100).padStart(2, '0')}`
  return neg ? `-${s}` : s
}

/** Percentual digitado ("12,5") → basis points (1250). Faixa 0–100%. */
export function parsePercentToBps(input: string | null | undefined): number | null {
  if (input === null || input === undefined) return null
  const s = String(input).replace('%', '').replace(/\s/g, '').trim()
  const m = /^(\d{1,3})(?:[,.](\d{1,2}))?$/.exec(s)
  if (!m) return null
  const bps = Number(m[1]) * 100 + Number((m[2] ?? '').padEnd(2, '0') || '0')
  return bps >= 0 && bps <= 10000 ? bps : null
}

export function bpsToInput(bps: number | null | undefined): string {
  if (bps === null || bps === undefined || !Number.isFinite(bps)) return ''
  const s = `${Math.floor(bps / 100)},${String(bps % 100).padStart(2, '0')}`
  return s.endsWith(',00') ? s.slice(0, -3) : s
}
