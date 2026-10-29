/**
 * Badge de status com palavra + cor + ícone (DS §10). "Online" é status de comunicação —
 * não prova reprodução (DS §10/§17). Presets em ./status-presets.
 */
import * as React from 'react'
import { Badge } from '@/components/ui/badge'
import { STATUS_PRESETS, type StatusKind } from './status-presets'

export type { StatusKind }

export interface StatusBadgeProps extends Omit<React.HTMLAttributes<HTMLSpanElement>, 'children'> {
  status: StatusKind
  /** Sobrescreve a palavra (ex.: "Ativo" no masculino). */
  label?: string
}

export function StatusBadge({ status, label, ...props }: StatusBadgeProps) {
  const preset = STATUS_PRESETS[status]
  const Icon = preset.icon
  return (
    <Badge tone={preset.tone} data-status={status} {...props}>
      <Icon aria-hidden="true" />
      {label ?? preset.label}
    </Badge>
  )
}
