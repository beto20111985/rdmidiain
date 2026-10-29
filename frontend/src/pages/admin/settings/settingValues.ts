/**
 * Conversões de valores de configuração (registry tipado do backend: int, str, bool, enum,
 * list[int]) entre o campo de texto e o JSON da API, e descrição legível por unidade
 * (bps → %, centavos → R$, horas, dias, segundos).
 */
import { formatBps, formatCents, formatNumber } from '@/lib/format'
import type { SettingItem } from '../types'

const ENUM_LABELS: Record<string, string> = {
  MANUAL: 'Manual (RD revisa)',
  AUTO: 'Automática',
  A_POOL: 'A — pool fixo',
  B_ATTRIBUTED: 'B — receita atribuída',
  COBRAR: 'Cobrar',
  BLOQUEAR: 'Bloquear',
}

export function settingInputValue(value: unknown): string {
  if (Array.isArray(value)) return value.join(', ')
  if (value === null || value === undefined) return ''
  return String(value)
}

export type Parsed = { ok: true; value: unknown } | { ok: false; error: string }

export function parseSettingInput(s: Pick<SettingItem, 'type' | 'min' | 'max' | 'choices'>, input: string): Parsed {
  const range = (n: number): string | null => {
    if (s.min !== null && s.min !== undefined && n < s.min) return `Mínimo ${s.min}.`
    if (s.max !== null && s.max !== undefined && n > s.max) return `Máximo ${s.max}.`
    return null
  }
  switch (s.type) {
    case 'int': {
      const t = input.trim()
      if (!/^-?\d+$/.test(t)) return { ok: false, error: 'Informe um número inteiro.' }
      const n = Number(t)
      const r = range(n)
      return r ? { ok: false, error: r } : { ok: true, value: n }
    }
    case 'bool':
      return { ok: true, value: input === 'true' }
    case 'enum':
      return s.choices?.includes(input) ? { ok: true, value: input } : { ok: false, error: 'Escolha uma das opções.' }
    case 'list[int]': {
      const parts = input.split(/[,;\s]+/).filter(Boolean)
      if (!parts.length) return { ok: false, error: 'Informe ao menos um número.' }
      const nums: number[] = []
      for (const p of parts) {
        if (!/^\d+$/.test(p)) return { ok: false, error: 'Use números inteiros separados por vírgula (ex.: 7, 28).' }
        const n = Number(p)
        const r = range(n)
        if (r) return { ok: false, error: r }
        nums.push(n)
      }
      const sorted = [...nums].sort((a, b) => a - b)
      if (sorted.join() !== nums.join() || new Set(nums).size !== nums.length)
        return { ok: false, error: 'Use valores crescentes e sem repetição.' }
      return { ok: true, value: nums }
    }
    default:
      return { ok: true, value: input }
  }
}

export function unitHint(s: Pick<SettingItem, 'key' | 'min' | 'max'>): string | null {
  const range = s.min !== null && s.max !== null ? ` (de ${s.min} a ${s.max})` : ''
  if (s.key.endsWith('_bps')) return `Em basis points: 100 = 1%${range}.`
  if (s.key.endsWith('_cents')) return `Em centavos: 1000 = R$ 10,00${range}.`
  if (s.key.endsWith('_hours') || s.key.includes('_hours_')) return `Em horas${range}.`
  if (s.key.endsWith('_days')) return `Em dias${range}.`
  if (s.key.endsWith('_s')) return `Em segundos${range}; múltiplos de 30 quando for ciclo.`
  return range ? `Valores aceitos${range}.` : null
}

export function describeValue(key: string, value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (Array.isArray(value)) return key.endsWith('_days') ? `${value.join(' e ')} dias` : value.join(', ')
  if (typeof value === 'boolean') return value ? 'Sim' : 'Não'
  if (typeof value === 'number') {
    if (key.endsWith('_bps')) return `${formatBps(value)}`
    if (key.endsWith('_cents')) return formatCents(value)
    if (key.endsWith('_hours') || key.includes('_hours_')) return `${formatNumber(value)} h`
    if (key.endsWith('_days')) return `${formatNumber(value)} dias`
    if (key.endsWith('_s')) return `${formatNumber(value)} s`
    return formatNumber(value)
  }
  if (typeof value === 'string') return ENUM_LABELS[value] ?? value
  return JSON.stringify(value)
}
