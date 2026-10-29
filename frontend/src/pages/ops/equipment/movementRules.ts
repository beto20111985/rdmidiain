import { MOVEMENT_RULES } from '@/pages/ops/api/labels'
import type { EquipmentItem, MovementType } from '@/pages/ops/api/types'

/** Movimentações possíveis para o item (matriz do backend + situação atual). */
export function allowedMovements(item: EquipmentItem): MovementType[] {
  const all = Object.keys(MOVEMENT_RULES) as MovementType[]
  if (item.tracking === 'BULK') return all
  if (item.status === 'WRITTEN_OFF') return []
  const cur = item.location?.kind
  return all.filter((t) => t !== 'ADJUSTMENT' && !!cur && MOVEMENT_RULES[t].from.includes(cur))
}
