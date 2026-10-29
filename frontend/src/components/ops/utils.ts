/** Utilidades puras das páginas de Operação (FE-OPS). */

export function osmUrl(lat: number, lng: number) {
  return `https://www.openstreetmap.org/?mlat=${lat.toFixed(6)}&mlon=${lng.toFixed(6)}#map=17/${lat.toFixed(6)}/${lng.toFixed(6)}`
}

/** "" / espaços → null (o backend distingue ausência de valor). */
export function emptyToNull(v: string | null | undefined): string | null {
  if (v === null || v === undefined) return null
  const t = v.trim()
  return t ? t : null
}

/** Converte "1.234,56" / "1234.56" / "1234" (reais) em centavos inteiros; inválido → NaN. */
export function parseBrlToCents(raw: string | null | undefined): number | null {
  if (raw === null || raw === undefined || raw.trim() === '') return null
  let s = raw.replace(/[R$\s]/g, '')
  if (s.includes(',')) s = s.replace(/\./g, '').replace(',', '.')
  if (!/^\d+(\.\d{1,2})?$/.test(s)) return Number.NaN
  const [int, dec = ''] = s.split('.')
  return Number(int) * 100 + Number((dec + '00').slice(0, 2))
}

/** Centavos → "1234,56" para preencher campos de valor (sem símbolo). */
export function centsToInput(cents: number | null | undefined): string {
  if (cents === null || cents === undefined || !Number.isFinite(cents)) return ''
  const neg = cents < 0
  const abs = Math.abs(Math.trunc(cents))
  return `${neg ? '-' : ''}${Math.floor(abs / 100)},${String(abs % 100).padStart(2, '0')}`
}

/** Remove máscara de CPF/CNPJ/CEP/telefone (o backend normaliza e valida os dígitos). */
export function onlyDigitsAndLetters(v: string): string {
  return v.replace(/[^0-9A-Za-z]/g, '').toUpperCase()
}

/** Código de pareamento XXXX-XXXX (alfabeto seguro do backend, sem 0/O/1/I/L/K/W/X). */
export const PAIRING_ALPHABET = 'ABCDEFGHJMNPQRSTUVYZ23456789'

export function normalizePairingCode(raw: string): string | null {
  const s = (raw || '').toUpperCase().replace(/[\s-]/g, '')
  if (s.length !== 8) return null
  for (const c of s) if (!PAIRING_ALPHABET.includes(c)) return null
  return `${s.slice(0, 4)}-${s.slice(4)}`
}

/** Formata enquanto digita: "abcd1234" → "ABCD-1234". */
export function maskPairingInput(raw: string): string {
  const s = (raw || '').toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 8)
  return s.length > 4 ? `${s.slice(0, 4)}-${s.slice(4)}` : s
}

/** Código RD do anúncio (criativo): RD-XXXXXX. */
export const CREATIVE_CODE_RE = /^RD-[ABCDEFGHJMNPQRSTUVYZ23456789]{6}$/

/** Tela TEL-UF-XXXX. */
export const SCREEN_CODE_RE = /^TEL-[A-Z]{2}-[ABCDEFGHJMNPQRSTUVYZ23456789]{4}$/

/** Monta query params removendo vazios e mapeando `q` (DataTable) → `search` (API W-ORG). */
export function toApiQuery(params: Record<string, string | number | undefined>): Record<string, string | number | undefined> {
  const { q, ...rest } = params
  return { ...rest, search: q || undefined }
}

// Conversões hora de parede ↔ UTC: utilitário único em '@/lib/dates'.
