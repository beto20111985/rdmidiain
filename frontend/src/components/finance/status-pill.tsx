/** Badge de estado (palavra + cor + ícone — DS §10) a partir de um mapa de `labels.ts`. */
import type { ReactNode } from 'react'
import { AlertCircle, CheckCircle2, CircleDashed, Clock, Info } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import type { StatusTone } from '@/components/patterns/status-presets'
import { statusEntry, type StatusMap } from './labels'

const ICON: Record<StatusTone, typeof Info> = {
  success: CheckCircle2,
  info: Info,
  warning: Clock,
  danger: AlertCircle,
  neutral: CircleDashed,
}

export function StatusPill({ map, value, className }: { map: StatusMap; value: string | null | undefined; className?: string }) {
  const e = statusEntry(map, value)
  const Icon = ICON[e.tone]
  return (
    <Badge tone={e.tone} data-status={value ?? undefined} className={className}>
      <Icon aria-hidden="true" />
      {e.label}
    </Badge>
  )
}

export function ToneBadge({ tone, children, className }: { tone: StatusTone; children: ReactNode; className?: string }) {
  const Icon = ICON[tone]
  return (
    <Badge tone={tone} className={className}>
      <Icon aria-hidden="true" />
      {children}
    </Badge>
  )
}
