/** Buscas para o EntityPicker (somente telas da equipe RD). */
import type { PickerOption } from '@/components/campaigns/entity-picker'
import { campaignsApi, creativesApi, lookupApi, taxonomyApi } from './api'
import { campaignStatusLabel } from './labels'

export async function searchOrganizations(q: string, role?: string): Promise<PickerOption[]> {
  const r = await lookupApi.organizations({ search: q || undefined, role, page_size: 20 })
  return r.items.map((o) => ({ id: o.id, label: o.trade_name || o.name, description: o.document_formatted ?? undefined }))
}

export async function searchEstablishments(q: string): Promise<PickerOption[]> {
  const r = await lookupApi.establishments({ search: q || undefined, page_size: 20 })
  return r.items.map((e) => ({
    id: e.id,
    label: e.name,
    description: [e.city, e.state].filter(Boolean).join(' · ') || undefined,
    timezone: e.timezone ?? null,
  }))
}

export async function searchCampaigns(q: string): Promise<PickerOption[]> {
  const r = await campaignsApi.list({ q: q || undefined, page_size: 20 })
  return r.items.map((c) => ({ id: c.id, label: c.name, description: campaignStatusLabel(c.status) }))
}

export async function searchCreatives(q: string): Promise<PickerOption[]> {
  const r = await creativesApi.list({ q: q || undefined, page_size: 20 })
  return r.items.map((c) => ({ id: c.id, label: c.code, description: c.name }))
}

export async function searchBrands(q: string): Promise<PickerOption[]> {
  const r = await taxonomyApi.brands({ search: q || undefined, page_size: 20 })
  return r.items.map((b) => ({ id: b.id, label: b.name }))
}

export async function searchProducts(q: string): Promise<PickerOption[]> {
  const r = await taxonomyApi.products({ search: q || undefined, page_size: 20 })
  return r.items.map((p) => ({ id: p.id, label: p.name }))
}

export async function searchScreens(q: string): Promise<PickerOption[]> {
  const r = await lookupApi.screens({ search: q || undefined, page_size: 20 })
  return r.items.map((s) => ({
    id: s.id,
    label: s.friendly_id,
    description: [s.name, s.establishment?.name, s.establishment?.state].filter(Boolean).join(' · ') || undefined,
    timezone: s.establishment?.timezone ?? null,
  }))
}
