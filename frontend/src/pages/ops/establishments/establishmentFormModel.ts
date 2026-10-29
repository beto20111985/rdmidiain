/** Modelo do formulário de estabelecimento: validação (zod), valores iniciais e corpo da API. */
import { z } from 'zod'
import { centsToInput, emptyToNull, parseBrlToCents } from '@/components/ops/utils'
import type { Establishment } from '@/pages/ops/api/types'

export function isValidTimeZone(tz: string): boolean {
  try {
    new Intl.DateTimeFormat('pt-BR', { timeZone: tz })
    return true
  } catch {
    return false
  }
}

const optStr = (max: number) => z.string().max(max, `Máximo de ${max} caracteres.`)

export const establishmentSchema = z
  .object({
    mode: z.enum(['create', 'edit']),
    org_id: z.string().nullable(),
    name: z.string().trim().min(2, 'Informe o nome (mínimo 2 caracteres).').max(255),
    trade_name: optStr(255),
    legal_name: optStr(255),
    document_type: z.enum(['CPF', 'CNPJ']).nullable(),
    document: optStr(20),
    responsible_name: optStr(255),
    phone: optStr(30),
    email: z.string().trim().email('E-mail inválido.').or(z.literal('')),
    postal_code: optStr(10),
    address: optStr(255),
    number: optStr(20),
    complement: optStr(100),
    neighborhood: optStr(100),
    city: z.string().trim().min(2, 'Informe a cidade.').max(100),
    state: z.string().length(2, 'Selecione a UF.'),
    city_ibge_code: z.string().regex(/^\d{7}$/, 'Código IBGE tem 7 dígitos.').or(z.literal('')),
    timezone: optStr(64),
    lat: z.string(),
    lng: z.string(),
    geo_source: z.enum(['MANUAL', 'GPS']),
    geo_accuracy_m: z.number().nullable(),
    category_id: z.string().nullable(),
    venue_class: z.enum(['STANDARD', 'PREMIUM']),
    weight: z.string(),
    partner_plan_id: z.string().nullable(),
    receives_commission: z.boolean(),
    pays_monthly_fee: z.boolean(),
    monthly_fee: z.string(),
    responsible_technician_id: z.string().nullable(),
    installation_date: z.string(),
    notes: optStr(4000),
    reason: optStr(2000),
  })
  .superRefine((v, ctx) => {
    if (v.mode === 'create' && !v.org_id) ctx.addIssue({ code: 'custom', path: ['org_id'], message: 'Selecione a empresa.' })
    const doc = v.document.replace(/[^0-9A-Za-z]/g, '')
    if (doc) {
      if (v.document_type === 'CPF' && !/^\d{11}$/.test(doc))
        ctx.addIssue({ code: 'custom', path: ['document'], message: 'CPF deve ter 11 dígitos.' })
      if (v.document_type === 'CNPJ' && !/^[0-9A-Z]{12}\d{2}$/i.test(doc))
        ctx.addIssue({ code: 'custom', path: ['document'], message: 'CNPJ deve ter 14 caracteres (12 letras/números + 2 dígitos).' })
      if (!v.document_type) ctx.addIssue({ code: 'custom', path: ['document_type'], message: 'Informe se é CPF ou CNPJ.' })
    }
    if (v.timezone.trim() && !isValidTimeZone(v.timezone.trim()))
      ctx.addIssue({ code: 'custom', path: ['timezone'], message: 'Fuso IANA inválido (ex.: America/Sao_Paulo).' })
    const hasLat = v.lat.trim() !== ''
    const hasLng = v.lng.trim() !== ''
    if (hasLat !== hasLng) ctx.addIssue({ code: 'custom', path: [hasLat ? 'lng' : 'lat'], message: 'Informe latitude e longitude juntas.' })
    if (hasLat) {
      const lat = Number(v.lat.replace(',', '.'))
      if (!Number.isFinite(lat) || lat < -90 || lat > 90) ctx.addIssue({ code: 'custom', path: ['lat'], message: 'Latitude entre -90 e 90.' })
    }
    if (hasLng) {
      const lng = Number(v.lng.replace(',', '.'))
      if (!Number.isFinite(lng) || lng < -180 || lng > 180) ctx.addIssue({ code: 'custom', path: ['lng'], message: 'Longitude entre -180 e 180.' })
    }
    const w = Number(v.weight.replace(',', '.'))
    if (!Number.isFinite(w) || w < 0.001 || w > 100) ctx.addIssue({ code: 'custom', path: ['weight'], message: 'Peso entre 0,001 e 100.' })
    if (v.pays_monthly_fee) {
      const c = parseBrlToCents(v.monthly_fee)
      if (c === null || Number.isNaN(c)) ctx.addIssue({ code: 'custom', path: ['monthly_fee'], message: 'Informe a mensalidade (ex.: 99,90).' })
    } else if (v.monthly_fee.trim() && Number.isNaN(parseBrlToCents(v.monthly_fee) ?? 0)) {
      ctx.addIssue({ code: 'custom', path: ['monthly_fee'], message: 'Valor inválido (ex.: 99,90).' })
    }
  })

export type EstablishmentFormValues = z.infer<typeof establishmentSchema>

export const FIELDS = [
  'org_id', 'name', 'trade_name', 'legal_name', 'document_type', 'document', 'responsible_name', 'phone', 'email',
  'postal_code', 'address', 'number', 'complement', 'neighborhood', 'city', 'state', 'city_ibge_code', 'timezone',
  'lat', 'lng', 'category_id', 'venue_class', 'partner_plan_id', 'monthly_fee', 'responsible_technician_id',
  'installation_date', 'notes', 'reason',
] as const

export function toFormValues(e: Establishment | null): EstablishmentFormValues {
  return {
    mode: e ? 'edit' : 'create',
    org_id: e?.org_id ?? null,
    name: e?.name ?? '',
    trade_name: e?.trade_name ?? '',
    legal_name: e?.legal_name ?? '',
    document_type: (e?.document_type as 'CPF' | 'CNPJ' | null) ?? null,
    document: e?.document ?? '',
    responsible_name: e?.responsible_name ?? '',
    phone: e?.phone ?? '',
    email: e?.email ?? '',
    postal_code: e?.postal_code ?? '',
    address: e?.address ?? '',
    number: e?.number ?? '',
    complement: e?.complement ?? '',
    neighborhood: e?.neighborhood ?? '',
    city: e?.city ?? '',
    state: e?.state ?? '',
    city_ibge_code: e?.city_ibge_code ?? '',
    timezone: e?.timezone ?? '',
    lat: e?.lat !== null && e?.lat !== undefined ? String(e.lat) : '',
    lng: e?.lng !== null && e?.lng !== undefined ? String(e.lng) : '',
    geo_source: (e?.geo_source as 'MANUAL' | 'GPS' | null) ?? 'MANUAL',
    geo_accuracy_m: e?.geo_accuracy_m ?? null,
    category_id: e?.category_id ?? null,
    venue_class: (e?.venue_class as 'STANDARD' | 'PREMIUM' | null) ?? 'STANDARD',
    weight: String(((e?.weight_milli ?? 1000) / 1000).toLocaleString('pt-BR', { maximumFractionDigits: 3 })),
    partner_plan_id: e?.partner_plan_id ?? null,
    receives_commission: e?.receives_commission ?? true,
    pays_monthly_fee: e?.pays_monthly_fee ?? false,
    monthly_fee: centsToInput(e?.monthly_fee_cents ?? null),
    responsible_technician_id: e?.responsible_technician_id ?? null,
    installation_date: e?.installation_date ?? '',
    notes: e?.notes ?? '',
    reason: '',
  }
}

/** Corpo da API a partir do formulário (strings vazias → null; dinheiro em centavos; peso em milésimos). */
export function toApiBody(v: EstablishmentFormValues): Record<string, unknown> {
  const body: Record<string, unknown> = {
    name: v.name.trim(),
    trade_name: emptyToNull(v.trade_name),
    legal_name: emptyToNull(v.legal_name),
    document_type: v.document.trim() ? v.document_type : null,
    document: emptyToNull(v.document),
    responsible_name: emptyToNull(v.responsible_name),
    phone: emptyToNull(v.phone),
    email: emptyToNull(v.email),
    postal_code: emptyToNull(v.postal_code),
    address: emptyToNull(v.address),
    number: emptyToNull(v.number),
    complement: emptyToNull(v.complement),
    neighborhood: emptyToNull(v.neighborhood),
    city: v.city.trim(),
    state: v.state.toUpperCase(),
    city_ibge_code: emptyToNull(v.city_ibge_code),
    timezone: emptyToNull(v.timezone),
    category_id: v.category_id,
    venue_class: v.venue_class,
    weight_milli: Math.round(Number(v.weight.replace(',', '.')) * 1000),
    partner_plan_id: v.partner_plan_id,
    receives_commission: v.receives_commission,
    pays_monthly_fee: v.pays_monthly_fee,
    monthly_fee_cents: v.pays_monthly_fee ? parseBrlToCents(v.monthly_fee) : null,
    responsible_technician_id: v.responsible_technician_id,
    installation_date: emptyToNull(v.installation_date),
    notes: emptyToNull(v.notes),
  }
  if (v.mode === 'create') {
    body.org_id = v.org_id
    if (v.lat.trim() && v.lng.trim()) {
      body.lat = Number(v.lat.replace(',', '.'))
      body.lng = Number(v.lng.replace(',', '.'))
      body.geo_source = v.geo_source
      body.geo_accuracy_m = v.geo_accuracy_m
    }
  } else {
    body.reason = emptyToNull(v.reason)
  }
  return body
}

