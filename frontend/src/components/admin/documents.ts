/**
 * CPF/CNPJ no cliente (espelho de backend/app/modules/organizations/documents.py) — só para
 * validar ANTES do envio; o servidor continua sendo a autoridade (DOCUMENT_INVALID).
 * CNPJ alfanumérico (IN RFB 2.229/2024, vigente desde jul/2026): 12 primeiras posições
 * 0-9A-Z, 2 dígitos verificadores numéricos calculados com `charCode − 48`.
 */
export type DocumentType = 'CPF' | 'CNPJ'

export function stripDocument(raw: string | null | undefined): string {
  return (raw ?? '').replace(/[\s.\-/]/g, '').toUpperCase()
}

function cpfDv(base9: string): string {
  const dv = (base: string) => {
    const w = base.length + 1
    const s = base.split('').reduce((acc, c, i) => acc + Number(c) * (w - i), 0)
    const r = (s * 10) % 11
    return r === 10 ? 0 : r
  }
  const d1 = dv(base9)
  const d2 = dv(base9 + String(d1))
  return `${d1}${d2}`
}

export function isValidCpf(raw: string | null | undefined): boolean {
  const d = stripDocument(raw)
  if (!/^[0-9]{11}$/.test(d) || d === d[0].repeat(11)) return false
  return cpfDv(d.slice(0, 9)) === d.slice(9)
}

const W1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
const W2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]

export function cnpjDv(base12: string): string {
  const vals = base12.toUpperCase().split('').map((c) => c.charCodeAt(0) - 48)
  const dv = (v: number[], w: number[]) => {
    const r = v.reduce((acc, x, i) => acc + x * w[i], 0) % 11
    return r < 2 ? 0 : 11 - r
  }
  const d1 = dv(vals, W1)
  const d2 = dv([...vals, d1], W2)
  return `${d1}${d2}`
}

export function isValidCnpj(raw: string | null | undefined): boolean {
  const d = stripDocument(raw)
  if (!/^[0-9A-Z]{12}[0-9]{2}$/.test(d) || d === d[0].repeat(14)) return false
  return cnpjDv(d.slice(0, 12)) === d.slice(12)
}

/** Mensagem de erro (pt-BR) ou null quando válido. */
export function documentProblem(raw: string, type: DocumentType): string | null {
  const d = stripDocument(raw)
  if (!d) return type === 'CPF' ? 'Informe o CPF.' : 'Informe o CNPJ.'
  if (type === 'CPF') {
    if (d.length !== 11) return 'O CPF tem 11 dígitos.'
    return isValidCpf(d) ? null : 'CPF inválido (dígitos verificadores não conferem).'
  }
  if (d.length !== 14) return 'O CNPJ tem 14 posições (aceita letras nas 12 primeiras).'
  return isValidCnpj(d) ? null : 'CNPJ inválido (dígitos verificadores não conferem).'
}

export function formatDocument(raw: string | null | undefined, type: DocumentType | string | null | undefined): string {
  const d = stripDocument(raw)
  if (type === 'CPF' && d.length === 11) return `${d.slice(0, 3)}.${d.slice(3, 6)}.${d.slice(6, 9)}-${d.slice(9)}`
  if (type === 'CNPJ' && d.length === 14) return `${d.slice(0, 2)}.${d.slice(2, 5)}.${d.slice(5, 8)}/${d.slice(8, 12)}-${d.slice(12)}`
  return d
}
