import {
  AlertCircle,
  CalendarClock,
  CheckCircle2,
  CircleDashed,
  Clock,
  PauseCircle,
  RefreshCw,
  WifiOff,
  Wifi,
  XCircle,
  type LucideIcon,
} from 'lucide-react'

/**
 * Status do DS §10 (palavra + cor + ícone; nunca só cor):
 *   Ativa/Online → verde · Agendada → azul claro · Pendente/Sincronizando → amarelo ·
 *   Erro/Offline → vermelho · Pausada → cinza.
 */
export type StatusKind =
  | 'active'
  | 'online'
  | 'scheduled'
  | 'pending'
  | 'syncing'
  | 'error'
  | 'offline'
  | 'paused'
  | 'draft'
  | 'finished'
  | 'rejected'

export type StatusTone = 'success' | 'info' | 'warning' | 'danger' | 'neutral'

export const STATUS_PRESETS: Record<StatusKind, { label: string; tone: StatusTone; icon: LucideIcon }> = {
  active: { label: 'Ativa', tone: 'success', icon: CheckCircle2 },
  online: { label: 'Online', tone: 'success', icon: Wifi },
  scheduled: { label: 'Agendada', tone: 'info', icon: CalendarClock },
  pending: { label: 'Pendente', tone: 'warning', icon: Clock },
  syncing: { label: 'Sincronizando', tone: 'warning', icon: RefreshCw },
  error: { label: 'Erro', tone: 'danger', icon: AlertCircle },
  offline: { label: 'Offline', tone: 'danger', icon: WifiOff },
  paused: { label: 'Pausada', tone: 'neutral', icon: PauseCircle },
  draft: { label: 'Rascunho', tone: 'neutral', icon: CircleDashed },
  finished: { label: 'Encerrada', tone: 'neutral', icon: CheckCircle2 },
  rejected: { label: 'Rejeitada', tone: 'danger', icon: XCircle },
}
