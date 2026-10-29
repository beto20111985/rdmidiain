/** Aviso de dados possivelmente desatualizados quando o WebSocket cai (DS §10; sem polling). */
import { StaleData } from '@/components/patterns/states'
import { useRealtimeDegraded } from './hooks'

export function LiveStale({
  updatedAt,
  onRefresh,
  refreshing,
  className,
}: {
  updatedAt: number | string | Date | null | undefined
  onRefresh?: () => void
  refreshing?: boolean
  className?: string
}) {
  const { degraded, reason } = useRealtimeDegraded()
  if (!degraded || !updatedAt) return null
  return <StaleData updatedAt={updatedAt} reason={reason} onRefresh={onRefresh} refreshing={refreshing} className={className} />
}
