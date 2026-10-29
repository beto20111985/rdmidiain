/** Modelo do formulário de tela (POST /screens, PATCH /screens/{id} — extra="forbid" no backend). */
import { z } from 'zod'
import { emptyToNull } from '@/components/ops/utils'
import type { Screen } from '@/pages/ops/api/types'

const optStr = (max: number) => z.string().max(max, `Máximo de ${max} caracteres.`)
const optInt = (min: number, max: number, msg: string) =>
  z.string().refine((v) => v.trim() === '' || (/^\d+$/.test(v.trim()) && Number(v) >= min && Number(v) <= max), msg)

export const screenSchema = z
  .object({
    mode: z.enum(['create', 'edit']),
    establishment_id: z.string().nullable(),
    name: z.string().trim().min(2, 'Informe o nome da tela (mínimo 2 caracteres).').max(255),
    orientation: z.enum(['HORIZONTAL', 'VERTICAL']),
    display_type: z.enum(['TV', 'MONITOR', 'LED_PANEL', 'PROJECTOR', 'OTHER']),
    player_type: z.enum(['TVBOX', 'SMART_TV_APP', 'PC', 'BROWSER', 'OTHER']),
    hardware_brand: optStr(100),
    hardware_model: optStr(100),
    inches: optInt(1, 500, 'Polegadas entre 1 e 500.'),
    resolution_w: optInt(1, 16384, 'Largura entre 1 e 16384 px.'),
    resolution_h: optInt(1, 16384, 'Altura entre 1 e 16384 px.'),
    serial_number: optStr(100),
    asset_tag: optStr(100),
    player_brand: optStr(100),
    player_model: optStr(100),
    player_serial_number: optStr(100),
    tv_owner: z.enum(['RD', 'ESTABLISHMENT']),
    screen_class: z.enum(['STANDARD', 'PREMIUM']),
    weight: z.string(),
    programming_mode: z.enum(['ESTABLISHMENT_DEFAULT', 'CUSTOM']),
    notes: optStr(4000),
    reason: optStr(2000),
  })
  .superRefine((v, ctx) => {
    if (v.mode === 'create' && !v.establishment_id)
      ctx.addIssue({ code: 'custom', path: ['establishment_id'], message: 'Selecione o estabelecimento.' })
    const w = Number(v.weight.replace(',', '.'))
    if (!Number.isFinite(w) || w < 0.001 || w > 100) ctx.addIssue({ code: 'custom', path: ['weight'], message: 'Peso entre 0,001 e 100.' })
  })

export type ScreenFormValues = z.infer<typeof screenSchema>

export const SCREEN_FIELDS = [
  'establishment_id', 'name', 'orientation', 'display_type', 'player_type', 'hardware_brand', 'hardware_model', 'inches',
  'resolution_w', 'resolution_h', 'serial_number', 'asset_tag', 'player_brand', 'player_model', 'player_serial_number',
  'tv_owner', 'screen_class', 'weight', 'programming_mode', 'notes', 'reason',
] as const

const s = (v: unknown) => (v === null || v === undefined ? '' : String(v))

export function screenToForm(x: Screen | null, establishmentId?: string | null): ScreenFormValues {
  return {
    mode: x ? 'edit' : 'create',
    establishment_id: x?.establishment_id ?? establishmentId ?? null,
    name: x?.name ?? '',
    orientation: x?.orientation ?? 'HORIZONTAL',
    display_type: (x?.display_type as ScreenFormValues['display_type']) ?? 'TV',
    player_type: (x?.player_type as ScreenFormValues['player_type']) ?? 'TVBOX',
    hardware_brand: s(x?.hardware.brand),
    hardware_model: s(x?.hardware.model),
    inches: s(x?.hardware.inches),
    resolution_w: x ? s(x.hardware.resolution_w) : '1920',
    resolution_h: x ? s(x.hardware.resolution_h) : '1080',
    serial_number: s(x?.hardware.serial_number),
    asset_tag: s(x?.hardware.asset_tag),
    player_brand: s(x?.hardware.player_brand),
    player_model: s(x?.hardware.player_model),
    player_serial_number: s(x?.hardware.player_serial_number),
    tv_owner: x?.tv_owner ?? 'ESTABLISHMENT',
    screen_class: x?.screen_class ?? 'STANDARD',
    weight: ((x?.weight_milli ?? 1000) / 1000).toLocaleString('pt-BR', { maximumFractionDigits: 3 }),
    programming_mode: x?.programming_mode ?? 'ESTABLISHMENT_DEFAULT',
    notes: s(x?.notes),
    reason: '',
  }
}

const intOrNull = (v: string) => (v.trim() === '' ? null : Number(v.trim()))

export function screenFormToBody(v: ScreenFormValues): Record<string, unknown> {
  const body: Record<string, unknown> = {
    name: v.name.trim(),
    orientation: v.orientation,
    display_type: v.display_type,
    player_type: v.player_type,
    hardware_brand: emptyToNull(v.hardware_brand),
    hardware_model: emptyToNull(v.hardware_model),
    inches: intOrNull(v.inches),
    resolution_w: intOrNull(v.resolution_w),
    resolution_h: intOrNull(v.resolution_h),
    serial_number: emptyToNull(v.serial_number),
    asset_tag: emptyToNull(v.asset_tag),
    player_brand: emptyToNull(v.player_brand),
    player_model: emptyToNull(v.player_model),
    player_serial_number: emptyToNull(v.player_serial_number),
    tv_owner: v.tv_owner,
    screen_class: v.screen_class,
    weight_milli: Math.round(Number(v.weight.replace(',', '.')) * 1000),
    programming_mode: v.programming_mode,
    notes: emptyToNull(v.notes),
  }
  if (v.mode === 'create') body.establishment_id = v.establishment_id
  else if (v.reason.trim()) body.reason = v.reason.trim()
  return body
}
