import { describe, expect, it } from 'vitest'
import {
  centsToInput,
  maskPairingInput,
  normalizePairingCode,
  parseBrlToCents,
  CREATIVE_CODE_RE,
  toApiQuery,
} from '@/components/ops/utils'
import { downloadPercent, lastContactOf } from '@/components/ops/presence-utils'
import { applyStatusEvent } from '../api/hooks'
import { presenceToSummary } from '../api/client'
import { connectionTestFresh } from '../installations/installationRules'
import { allowedMovements } from '../equipment/movementRules'
import { rotationValid } from '../plans/planRules'
import { docError } from '../onboarding/stepRules'
import { sanitizeProgramming } from '../unit/programming'
import { externalBlockStart } from '../screens/scheduleUtils'
import { establishmentSchema, toApiBody, toFormValues } from '../establishments/establishmentFormModel'
import type { EquipmentItem } from '../api/types'
import { installationPatchBody, installationPatchError } from '../installations/installationRules'
import { ApiError } from '@/lib/errors'

describe('dinheiro e códigos', () => {
  it('converte reais digitados em centavos inteiros (nunca float)', () => {
    expect(parseBrlToCents('1.299,90')).toBe(129990)
    expect(parseBrlToCents('99,9')).toBe(9990)
    expect(parseBrlToCents('1234.56')).toBe(123456)
    expect(parseBrlToCents('R$ 10')).toBe(1000)
    expect(parseBrlToCents('')).toBeNull()
    expect(Number.isNaN(parseBrlToCents('abc') as number)).toBe(true)
    expect(centsToInput(129990)).toBe('1299,90')
    expect(centsToInput(null)).toBe('')
  })

  it('código de pareamento: normaliza, mascara e recusa caracteres fora do alfabeto seguro', () => {
    expect(normalizePairingCode('abcd-2345')).toBe('ABCD-2345')
    expect(normalizePairingCode(' abcd 2345 ')).toBe('ABCD-2345')
    expect(normalizePairingCode('ABCD-234')).toBeNull()
    expect(normalizePairingCode('ABC0-2345')).toBeNull() // 0 fora do alfabeto
    expect(normalizePairingCode('ABCI-2345')).toBeNull() // I fora do alfabeto
    expect(maskPairingInput('abcd2345xyz')).toBe('ABCD-2345')
  })

  it('código RD do anúncio no formato RD-XXXXXX', () => {
    expect(CREATIVE_CODE_RE.test('RD-ABC234')).toBe(true)
    expect(CREATIVE_CODE_RE.test('RD-ABC23')).toBe(false)
    expect(CREATIVE_CODE_RE.test('RD-ABCO23')).toBe(false)
  })

  it('busca da tabela vira `search` (contrato W-ORG)', () => {
    expect(toApiQuery({ q: 'pizza', page: 1 })).toEqual({ page: 1, search: 'pizza' })
  })
})

describe('presença', () => {
  it('último contato e % de download vêm da API; sem dado → null', () => {
    expect(lastContactOf({ last_seen_ms: 1_700_000_000_000 })?.toISOString()).toBe('2023-11-14T22:13:20.000Z')
    expect(lastContactOf({ last_seen_ms: null, last_seen_at: null })).toBeNull()
    expect(downloadPercent({ paired: true, status: 'sincronizando', download: { done_bytes: 50, total_bytes: 200 } })).toBe(25)
    expect(downloadPercent({ paired: true, status: 'sincronizando', download: null })).toBeNull()
    expect(downloadPercent({ paired: true, status: 'sincronizando', download: { done_bytes: 1, total_bytes: 0 } })).toBeNull()
  })

  it('evento screen.status atualiza o resumo sem perder campos', () => {
    const prev = { paired: true, status: 'online', app_version: '1.2.0', manifest_version_applied: 7 }
    const next = applyStatusEvent(prev, { screen_id: 's1', status: 'offline', last_seen_ms: 10 })
    expect(next).toMatchObject({ status: 'offline', app_version: '1.2.0', manifest_version_applied: 7, last_seen_ms: 10 })
    const pend = applyStatusEvent(next, { screen_id: 's1', status: 'sincronizando', manifest_version_pending: 8 })
    expect(pend.manifest_version_pending).toBe(8)
  })

  it('presença por tela vira resumo (não pareada → offline)', () => {
    expect(presenceToSummary({ screen_id: 's', screen_code: null, paired: false, device: null, live: { status: 'offline' } })).toMatchObject({
      paired: false,
      status: 'offline',
    })
  })
})

describe('regras de domínio espelhadas do backend', () => {
  it('teste de conexão só vale ONLINE e com até 30 min', () => {
    const now = Date.parse('2026-10-07T12:00:00Z')
    expect(connectionTestFresh({ status: 'ONLINE', checked_at: '2026-10-07T11:45:00+00:00' }, now)).toBe(true)
    expect(connectionTestFresh({ status: 'ONLINE', checked_at: '2026-10-07T11:00:00+00:00' }, now)).toBe(false)
    expect(connectionTestFresh({ status: 'OFFLINE', checked_at: '2026-10-07T11:59:00+00:00' }, now)).toBe(false)
  })

  it('movimentações possíveis seguem a situação atual do item com série', () => {
    const base = { tracking: 'SERIAL', status: 'IN_STOCK', location: { kind: 'WAREHOUSE' } } as unknown as EquipmentItem
    const m = allowedMovements(base)
    expect(m).toContain('INSTALL')
    expect(m).toContain('COMODATO')
    expect(m).not.toContain('RETURN')
    expect(m).not.toContain('ADJUSTMENT')
    expect(allowedMovements({ ...base, location: { kind: 'SCREEN' } } as unknown as EquipmentItem)).toEqual(
      expect.arrayContaining(['RETURN', 'MAINTENANCE_IN', 'WRITE_OFF']),
    )
    expect(allowedMovements({ ...base, status: 'WRITTEN_OFF' } as EquipmentItem)).toEqual([])
    expect(allowedMovements({ ...base, tracking: 'BULK' } as EquipmentItem)).toContain('ADJUSTMENT')
  })

  it('rotação do plano: múltiplos de 30 s e ciclo divisor de 1 h', () => {
    expect(rotationValid(120, 120)).toBe(true)
    expect(rotationValid(180, 60)).toBe(true)
    expect(rotationValid(150, 120)).toBe(false) // 270 s não divide 3600
    expect(rotationValid(120, 45)).toBe(false)
  })

  it('CPF/CNPJ (inclusive alfanumérico)', () => {
    expect(docError('123.456.789-09', 'CPF', true)).toBeNull()
    expect(docError('12.ABC.345/01DE-35', 'CNPJ', true)).toBeNull()
    expect(docError('123', 'CPF', true)).toMatch(/11 dígitos/)
    expect(docError('', 'CNPJ', true)).toMatch(/Informe/)
    expect(docError('', 'CNPJ', false)).toBeNull()
  })

  it('início do bloco externo pela âncora (RDPP/1 §5)', () => {
    // âncora 0, ciclo 240 s, interno 120 s → bucket 240 000 tem externo em 360 000
    expect(externalBlockStart(240_000, 0, 240_000, 120_000)).toBe(360_000)
    expect(externalBlockStart(240_000, null, 240_000, 120_000)).toBeNull()
  })
})

describe('visibilidade: programação do estabelecimento', () => {
  it('remove qualquer dado comercial além de código e tipo', () => {
    // contrato real de GET /scheduler/screens/{id}/programming + campos intrusos que não podem vazar
    const raw = {
      screen_id: 's',
      screen_code: 'TEL-SP-ABCD',
      timezone: 'America/Sao_Paulo',
      rotation: { internal_ms: 120000, external_ms: 120000, cycle_ms: 240000, unit_ms: 30000, policy_id: 'pol' },
      anchor_ms: 0,
      from_ms: 0,
      to_ms: 3_600_000,
      blocks: [
        {
          bucket_ms: 1,
          entries: [{ code: 'RD-ABC234', kind: 'EXTERNAL', campaign_id: 'c1', advertiser_name: 'Anunciante X', price_cents: 999 }],
        },
      ],
      internal_pattern: [[{ code: 'RD-INT222', name: 'Cardápio', duration_ms: 30000, org_id: 'o1' }]],
    }
    const out = sanitizeProgramming(raw as never)
    const json = JSON.stringify(out)
    expect(json).not.toContain('Anunciante X')
    expect(json).not.toContain('campaign_id')
    expect(json).not.toContain('price_cents')
    expect(json).not.toContain('org_id')
    expect(json).not.toContain('policy_id')
    expect(out.blocks[0].entries[0]).toEqual({ code: 'RD-ABC234', kind: 'EXTERNAL' })
    expect(out.internal_pattern[0][0]).toEqual({ code: 'RD-INT222', name: 'Cardápio', duration_ms: 30000 })
  })
})

describe('formulário de estabelecimento', () => {
  it('valida obrigatórios, fuso IANA, par de coordenadas e mensalidade', () => {
    const v = { ...toFormValues(null), name: 'X', city: '', state: '', timezone: 'Lua/Base', lat: '-23.5', lng: '', pays_monthly_fee: true, monthly_fee: '' }
    const r = establishmentSchema.safeParse(v)
    expect(r.success).toBe(false)
    const paths = r.success ? [] : r.error.issues.map((i) => i.path[0])
    expect(paths).toEqual(expect.arrayContaining(['org_id', 'name', 'city', 'state', 'timezone', 'lng', 'monthly_fee']))
  })

  it('monta o corpo com centavos, peso em milésimos e nulos (sem strings vazias)', () => {
    const body = toApiBody({
      ...toFormValues(null),
      org_id: 'org-1',
      name: 'Padaria Central',
      city: 'Campinas',
      state: 'SP',
      weight: '1,5',
      pays_monthly_fee: true,
      monthly_fee: '49,90',
      lat: '-22.9',
      lng: '-47.06',
    })
    expect(body).toMatchObject({ org_id: 'org-1', weight_milli: 1500, monthly_fee_cents: 4990, lat: -22.9, lng: -47.06, geo_source: 'MANUAL' })
    expect(body.trade_name).toBeNull()
    expect(body.timezone).toBeNull() // servidor sugere pelo endereço
  })
})

describe('PATCH de instalação — só campos alterados e erros do contrato', () => {
  const inst = { screen_id: null, technician_user_id: 'u1', scheduled_date: '2026-10-08', notes: null }
  it('nada alterado → corpo vazio; vincular → só screen_id; desvincular → screen_id null', () => {
    expect(installationPatchBody(inst, { screenId: null, techId: 'u1', date: '2026-10-08', notes: '' })).toEqual({})
    expect(installationPatchBody(inst, { screenId: 's2', techId: 'u1', date: '2026-10-08', notes: '  ' })).toEqual({ screen_id: 's2' })
    expect(installationPatchBody({ ...inst, screen_id: 's2' }, { screenId: null, techId: 'u1', date: '2026-10-08', notes: '' })).toEqual({ screen_id: null })
    expect(installationPatchBody(inst, { screenId: null, techId: 'u8', date: '', notes: 'levar escada' })).toEqual({
      technician_user_id: 'u8',
      scheduled_date: null,
      notes: 'levar escada',
    })
  })
  it('mensagens por código (404 = tela de outro estabelecimento)', () => {
    const err = (status: number, code: string) => new ApiError({ status, code, message: 'x', details: {} })
    expect(installationPatchError(err(404, 'NOT_FOUND'))).toMatch(/não pertence a este estabelecimento/)
    expect(installationPatchError(err(409, 'SCREEN_DECOMMISSIONED'))).toMatch(/retirada definitivamente/)
    expect(installationPatchError(err(409, 'INSTALLATION_INVALID_STATE'))).toMatch(/não pode mais ser alterada/)
    expect(installationPatchError(err(400, 'TECHNICIAN_REQUIRED'))).toMatch(/técnico responsável/)
  })
})
