/**
 * Dados de TESTE (somente Vitest) — formatos espelham as respostas reais do backend.
 * Nunca importados pelo código de produção.
 */
import { vi } from 'vitest'
import { configure } from '@testing-library/react'
import { useAuthStore } from '@/stores/authStore'
import { useContextStore } from '@/stores/contextStore'
import { makeMe } from '@/test/utils'
import type { AppContextInfo } from '@/features/auth/types'
import type { CampaignDetail, Quote } from '../lib/types'

// Os testes de tela rodam no mesmo host que as suítes do backend: sob carga, a espera padrão
// (1 s por `findBy`, 5 s por teste) gera falso negativo. Folga só de tempo — as asserções não mudam.
configure({ asyncUtilTimeout: 5000 })
vi.setConfig({ testTimeout: 30000 })

export const ADV_CONTEXT: AppContextInfo = { key: 'adv:org-1', label: 'Minhas campanhas — Loja Teste', kind: 'ADVERTISER', org_id: 'org-1' }
export const RD_CONTEXT: AppContextInfo = { key: 'rd', label: 'RD Mídia Indoor', kind: 'PLATFORM' }

export const ADVERTISER_PERMS = [
  'dashboard.view',
  'campaigns.view',
  'campaigns.manage',
  'creatives.view',
  'creatives.manage',
  'media.view',
  'media.upload',
  'media.delete',
  'checkout.create',
  'payments.view',
  'design.request',
  'reports.view',
  'inventory.view',
]

export const STAFF_PERMS = [
  ...ADVERTISER_PERMS,
  'campaigns.approve',
  'campaigns.kill',
  'establishments.view',
  'organizations.view',
  'taxonomy.manage',
  'conflicts.override',
  'pricing.view',
  'pricing.manage',
]

export function signInAs(kind: 'advertiser' | 'staff') {
  const contexts = [kind === 'advertiser' ? ADV_CONTEXT : RD_CONTEXT]
  const me = makeMe({ permissions: kind === 'advertiser' ? ADVERTISER_PERMS : STAFF_PERMS, contexts })
  useAuthStore.setState({ status: 'authenticated', me, demo: false })
  useContextStore.getState().reset()
  useContextStore.getState().syncFromMe(contexts)
}

export function campaignFixture(over: Partial<CampaignDetail> = {}): CampaignDetail {
  return {
    id: 'c1',
    org_id: 'org-1',
    name: 'Campanha de Teste',
    description: null,
    status: 'RASCUNHO',
    version: 3,
    sales_mode: 'SELF_SERVICE',
    plan_id: null,
    period_type: 'AVULSO',
    start_date: '2099-01-10',
    end_date: '2099-01-20',
    duration_s: 30,
    freq_kind: 'PER_HOUR',
    freq_value: 2,
    specific_times: [],
    min_interval_s: 0,
    days_mask: 127,
    time_bands: [[0, 1440]],
    priority: 'STANDARD',
    exclusivity: 'NONE',
    allow_same_advertiser_consecutive: false,
    request_art_service: false,
    auto_renew: false,
    product_id: null,
    brand_id: null,
    contracted_price_cents: 0,
    paid_at: null,
    approval_source: null,
    submitted_at: null,
    review_due_at: null,
    approved_at: null,
    rejected_at: null,
    rejection_reason: null,
    has_live_version: false,
    has_pending_change: false,
    playable: false,
    payment_issue: null,
    created_at: '2099-01-01T12:00:00Z',
    updated_at: '2099-01-01T12:00:00Z',
    targets: [{ type: 'STATE', value: 'GO', exclude: false }],
    segment_ids: [],
    creatives: [],
    allowed_transitions: { checkout: ['AGUARDANDO_PAGAMENTO'], cancel: ['CANCELADA'] },
    reproducing: { playing: null, last_valid_play_at: null, source: 'unavailable' },
    live_revision: null,
    pending_revision: null,
    ...over,
  }
}

export function quoteFixture(total = 123456): Quote {
  return {
    engine_version: 'test',
    currency: 'BRL',
    price_table: { version: 7 },
    inputs: {
      duration_s: 30,
      freq_kind: 'PER_HOUR',
      freq_value: 2,
      days_mask: 127,
      start_date: '2099-01-10',
      end_date: '2099-01-20',
      period_type: 'AVULSO',
      screens_count: 12,
      contracted_days: 11,
      calendar_days: 11,
      coupon_code: null,
    },
    insertions: { per_screen_per_day: '48', total: 6336 },
    lines: [
      { code: 'MEDIA', kind: 'MEDIA', amount_cents: total + 1000, description: 'Veiculação 30s em 12 tela(s), 11 dia(s)' },
      { code: 'COUPON', kind: 'DISCOUNT', amount_cents: -1000, description: 'Cupom TESTE' },
    ],
    groups: [],
    media_gross_cents: total + 1000,
    discount_cents: 1000,
    min_price_adjustment_cents: 0,
    media_cents: total,
    fees_cents: 0,
    total_cents: total,
    quote_hash: 'abcdef0123456789',
  }
}

export const availabilityFixture = {
  screens_available: 12,
  establishments_count: 5,
  screens_letterbox: 2,
  excluded_by_duration: 1,
  excluded_by_conflict_establishments: 3,
  by_region: [{ region: 'CENTRO_OESTE', screens: 12 }],
  by_state: [{ state: 'GO', screens: 12 }],
  by_city: [{ city_key: 'GO:goiania', screens: 12 }],
}

/** Corpos JSON de todas as chamadas feitas (para checar que nenhum preço sai do cliente). */
export function sentBodies(fetchMock: { mock: { calls: unknown[][] } }): Array<Record<string, unknown>> {
  return fetchMock.mock.calls
    .map((c) => (c[1] as RequestInit | undefined)?.body)
    .filter((b): b is string => typeof b === 'string')
    .map((b) => JSON.parse(b) as Record<string, unknown>)
}
