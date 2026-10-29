/** Aba "Audiência estimada" do estabelecimento — GET/POST /establishments/{id}/audience-estimates. */
import { AudiencePanel } from '@/components/ops/audience-panel'
import { useCan } from '@/hooks/usePermissions'
import { establishmentsApi } from '@/pages/ops/api/client'
import type { Establishment } from '@/pages/ops/api/types'

export function EstablishmentAudience({ est }: { est: Establishment }) {
  const can = useCan()
  return (
    <AudiencePanel
      current={est.estimated_daily_visitors}
      domain="establishments"
      subjectId={est.id}
      list={() => establishmentsApi.audience(est.id)}
      add={(body) => establishmentsApi.addAudience(est.id, body)}
      canEdit={can('establishments.manage') && est.is_active}
      timeZone={est.timezone}
    />
  )
}
