/**
 * Nome da organização anunciante (somente equipe RD — `organizations.view`). Sem permissão
 * ou sem dado → identificador curto, nunca um nome inventado.
 */
import { useQuery } from '@tanstack/react-query'
import { qk } from '@/lib/queryKeys'
import { useCan } from '@/hooks/usePermissions'
import { lookupApi } from '@/pages/campaigns/lib/api'
import { shortId } from '@/pages/campaigns/lib/format'
import { DEFAULT_TIMEZONE, formatDateTime } from '@/lib/format'
import { isTimeZone } from '@/lib/dates'

export function OrgName({ id }: { id: string | null | undefined }) {
  const can = useCan()
  const allowed = can('organizations.view') && !!id
  const q = useQuery({
    queryKey: qk.detail('organizations', id ?? '-'),
    queryFn: () => lookupApi.organization(id as string),
    enabled: allowed,
    staleTime: 10 * 60 * 1000,
  })
  if (!id) return <>—</>
  const name = q.data?.trade_name || q.data?.name
  return <span title={id}>{name ?? `Organização ${shortId(id)}`}</span>
}

export function EstablishmentName({ id }: { id: string | null | undefined }) {
  const can = useCan()
  const allowed = can('establishments.view') && !!id
  const q = useQuery({
    queryKey: qk.detail('establishments', id ?? '-'),
    queryFn: () => lookupApi.establishment(id as string),
    enabled: allowed,
    staleTime: 10 * 60 * 1000,
  })
  if (!id) return <>—</>
  return <span title={id}>{q.data?.name ?? `Estabelecimento ${shortId(id)}`}</span>
}

/**
 * Data/hora de algo que pertence a um estabelecimento (regra, exceção, override) no FUSO DELE —
 * a data é gravada como 23:59:59 local do estabelecimento e deve ser lida no mesmo fuso.
 * Fora do horário de Brasília, mostra a cidade do fuso. Sem permissão/dado: horário de Brasília.
 */
export function EstablishmentTime({ establishmentId, value }: { establishmentId: string; value: string }) {
  const can = useCan()
  const q = useQuery({
    queryKey: qk.detail('establishments', establishmentId),
    queryFn: () => lookupApi.establishment(establishmentId),
    enabled: can('establishments.view'),
    staleTime: 10 * 60 * 1000,
  })
  const tz = q.data?.timezone && isTimeZone(q.data.timezone) ? q.data.timezone : DEFAULT_TIMEZONE
  const place = tz === DEFAULT_TIMEZONE ? '' : ` (horário de ${tz.split('/').pop()?.replace(/_/g, ' ')})`
  return (
    <time dateTime={value} title={tz}>
      {formatDateTime(value, tz)}
      {place}
    </time>
  )
}
