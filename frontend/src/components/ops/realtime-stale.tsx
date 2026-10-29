/**
 * Aviso de dados possivelmente desatualizados quando o tempo real (WebSocket) cai (DS §10 Estados).
 * Não faz polling: oferece "Atualizar agora" (refetch manual).
 */
import { StaleData } from '@/components/patterns/states'
import { useRealtimeStatus } from '@/lib/realtime/context'

export function RealtimeStale({
  updatedAt,
  onRefresh,
  refreshing,
}: {
  updatedAt: number | string | Date | null | undefined
  onRefresh?: () => void
  refreshing?: boolean
}) {
  const { status } = useRealtimeStatus()
  if (status !== 'reconnecting' && status !== 'unavailable' && status !== 'closed') return null
  const reason =
    status === 'reconnecting'
      ? 'tempo real reconectando; o status das telas pode estar atrasado.'
      : 'tempo real indisponível; o status das telas pode estar desatualizado.'
  return <StaleData updatedAt={updatedAt || null} reason={reason} onRefresh={onRefresh} refreshing={refreshing} />
}
