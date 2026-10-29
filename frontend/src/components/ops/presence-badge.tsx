/**
 * Presença do player (W-PLAY) com palavra + cor + ícone (DS §10 Badges) e último contato.
 * "Online"/"Conectado" é status de COMUNICAÇÃO — não prova reprodução (DS §10/§17).
 * Tela offline: "Sem comunicação. Último contato: {data e hora}" (microcopy DS §17).
 */
import {
  AlertCircle,
  AlertOctagon,
  CheckCircle2,
  Link2Off,
  PlayCircle,
  PlugZap,
  RefreshCw,
  Wifi,
  WifiOff,
  type LucideIcon,
} from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { EMPTY_VALUE, formatDateTime, formatRelative } from '@/lib/format'
import { isPresenceStatus, PRESENCE_LABEL, PRESENCE_TONE } from '@/pages/ops/api/labels'
import type { PresenceStatus, PresenceSummary } from '@/pages/ops/api/types'
import { lastContactOf } from './presence-utils'
import { cn } from '@/lib/utils'

const ICON: Record<PresenceStatus, LucideIcon> = {
  conectado: PlugZap,
  online: Wifi,
  offline: WifiOff,
  sincronizando: RefreshCw,
  sincronizado: CheckCircle2,
  reproduzindo: PlayCircle,
  travado: AlertOctagon,
  erro: AlertCircle,
}

export function PresenceStatusBadge({ status }: { status: string | null | undefined }) {
  if (!status || !isPresenceStatus(status)) {
    return (
      <Badge tone="neutral" data-presence="desconhecido">
        <AlertCircle aria-hidden="true" />
        Sem informação
      </Badge>
    )
  }
  const Icon = ICON[status]
  return (
    <Badge tone={PRESENCE_TONE[status]} data-presence={status}>
      <Icon aria-hidden="true" />
      {PRESENCE_LABEL[status]}
    </Badge>
  )
}

export interface PresenceBadgeProps {
  presence: PresenceSummary | null | undefined
  /** Fuso do estabelecimento (dado de estabelecimento). */
  timeZone?: string
  /** Mostra a linha de último contato também quando online. */
  showLastContact?: boolean
  loading?: boolean
  className?: string
}

export function PresenceBadge({ presence, timeZone, showLastContact = false, loading, className }: PresenceBadgeProps) {
  if (loading) return <span className={cn('text-xs text-text-muted', className)}>Verificando…</span>
  if (!presence) return <span className={cn('text-text-muted', className)}>{EMPTY_VALUE}</span>
  if (!presence.paired) {
    return (
      <div className={cn('flex flex-col items-start gap-1', className)}>
        <Badge tone="neutral" data-presence="nao-pareada">
          <Link2Off aria-hidden="true" />
          Não pareada
        </Badge>
      </div>
    )
  }
  const last = lastContactOf(presence)
  const offline = presence.status === 'offline'
  return (
    <div className={cn('flex flex-col items-start gap-1', className)}>
      <PresenceStatusBadge status={presence.status} />
      {offline || showLastContact ? (
        <span className="text-xs text-text-muted">
          {offline ? 'Sem comunicação. ' : ''}Último contato:{' '}
          {last ? (
            <time dateTime={last.toISOString()} title={formatRelative(last)}>
              {formatDateTime(last, timeZone)}
            </time>
          ) : (
            EMPTY_VALUE
          )}
        </span>
      ) : null}
    </div>
  )
}
