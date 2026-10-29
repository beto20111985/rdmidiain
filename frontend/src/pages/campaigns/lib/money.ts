/**
 * Conversão de ENTRADA do usuário (texto pt-BR) para as unidades do contrato: centavos e bps.
 * Não calcula preço — apenas converte o que a equipe digita ao cadastrar tabelas/cupons.
 */
function parseDecimal(raw: string): number | null {
  const s = raw.trim().replace(/\s|R\$|%/g, '')
  if (!s) return null
  // "1.234,56" → 1234.56 ; "1234.56" → 1234.56 ; "12,5" → 12.5
  const normalized = s.includes(',') ? s.replace(/\./g, '').replace(',', '.') : s
  if (!/^-?\d+(\.\d+)?$/.test(normalized)) return null
  const n = Number(normalized)
  return Number.isFinite(n) ? n : null
}

/** "1.234,56" → 123456 (centavos inteiros); inválido → null. */
export function parseMoneyToCents(raw: string): number | null {
  const n = parseDecimal(raw)
  if (n === null) return null
  return Math.round(n * 100)
}

/** "12,5" (%) → 1250 bps. */
export function parsePercentToBps(raw: string): number | null {
  const n = parseDecimal(raw)
  if (n === null) return null
  return Math.round(n * 100)
}

/** "1,25" (×) → 12500 bps. */
export function parseMultiplierToBps(raw: string): number | null {
  const n = parseDecimal(raw)
  if (n === null) return null
  return Math.round(n * 10000)
}

export function centsToInput(cents: number | null | undefined): string {
  if (cents === null || cents === undefined) return ''
  return (cents / 100).toFixed(2).replace('.', ',')
}
export function bpsToPercentInput(bps: number | null | undefined): string {
  if (bps === null || bps === undefined) return ''
  return String(bps / 100).replace('.', ',')
}
export function bpsToMultiplierInput(bps: number | null | undefined): string {
  if (bps === null || bps === undefined) return ''
  return String(bps / 10000).replace('.', ',')
}

/** Valor de item de preço formatado conforme o modo. */
export function itemValueText(mode: string, value: number): string {
  const brl = new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' })
  switch (mode) {
    case 'MULTIPLIER_BPS':
      return `× ${(value / 10000).toLocaleString('pt-BR', { maximumFractionDigits: 4 })}`
    case 'PERCENT_BPS':
      return `${(value / 100).toLocaleString('pt-BR', { maximumFractionDigits: 2 })}%`
    case 'BASE_CENTS':
      return `${brl.format(value / 100)} por inserção por tela`
    case 'ADD_CENTS':
      return `+ ${brl.format(value / 100)} por inserção`
    case 'PER_SCREEN_MONTH_CENTS':
      return `${brl.format(value / 100)} por tela/mês`
    default:
      return brl.format(value / 100)
  }
}

/** Converte o texto digitado para o valor do modo (centavos ou bps). */
export function parseItemValue(mode: string, raw: string): number | null {
  if (mode === 'MULTIPLIER_BPS') return parseMultiplierToBps(raw)
  if (mode === 'PERCENT_BPS') return parsePercentToBps(raw)
  return parseMoneyToCents(raw)
}

export function itemValueToInput(mode: string, value: number): string {
  if (mode === 'MULTIPLIER_BPS') return bpsToMultiplierInput(value)
  if (mode === 'PERCENT_BPS') return bpsToPercentInput(value)
  return centsToInput(value)
}

export function itemValueLabel(mode: string): string {
  if (mode === 'MULTIPLIER_BPS') return 'Multiplicador (ex.: 1,25)'
  if (mode === 'PERCENT_BPS') return 'Percentual (%)'
  return 'Valor (R$)'
}
