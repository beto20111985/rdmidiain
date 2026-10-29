/**
 * Badges do domínio de campanhas (palavra + cor + ícone; nunca só cor — DS §10).
 * REPRODUZINDO é derivado de comprovação de exibição recente (PoP), não é status.
 */
import { AlertCircle, CheckCircle2, CircleDashed, Clock, PauseCircle, PlayCircle, RefreshCw, XCircle, type LucideIcon } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import type { StatusTone } from '@/components/patterns/status-presets'
import { CAMPAIGN_STATUS } from '@/pages/campaigns/lib/labels'
import type { CampaignStatus, PlayingState } from '@/pages/campaigns/lib/types'
import { formatDateTime } from '@/lib/format'

const TONE_ICON: Record<StatusTone, LucideIcon> = {
  success: CheckCircle2,
  info: Clock,
  warning: RefreshCw,
  danger: AlertCircle,
  neutral: CircleDashed,
}

export function ToneBadge({ label, tone, icon }: { label: string; tone: StatusTone; icon?: LucideIcon }) {
  const Icon = icon ?? TONE_ICON[tone]
  return (
    <Badge tone={tone}>
      <Icon aria-hidden="true" />
      {label}
    </Badge>
  )
}

export function CampaignStatusBadge({ status }: { status: CampaignStatus | string }) {
  const s = (CAMPAIGN_STATUS as Record<string, { label: string; tone: StatusTone }>)[status]
  const icon = status === 'PAUSADA' ? PauseCircle : status === 'REJEITADA' || status === 'CANCELADA' ? XCircle : undefined
  return <ToneBadge label={s?.label ?? status} tone={s?.tone ?? 'neutral'} icon={icon} />
}

/**
 * REPRODUZINDO: usa o `display_status` do servidor (ATIVA + exibição comprovada na janela).
 * `is_reproducing === null` (playback indisponível) ou status diferente → nada (nunca inventa).
 */
export function PlayingBadge({
  campaign,
}: {
  campaign: { display_status?: string; is_reproducing?: boolean | null; reproducing?: PlayingState | null }
}) {
  if (campaign.is_reproducing === null || campaign.display_status !== 'REPRODUZINDO') return null
  const last = campaign.reproducing?.last_valid_play_at
  return (
    <span data-testid="reproducing-badge" title={last ? `Última exibição comprovada: ${formatDateTime(last)}` : undefined}>
      <ToneBadge label="Reproduzindo" tone="success" icon={PlayCircle} />
    </span>
  )
}
