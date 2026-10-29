/**
 * Indicador de tempo real + "Atualizado às HH:MM" (DS §9, E2 #772, #820–829).
 * Não faz requisições: reflete o estado do RealtimeProvider.
 */
import { useRealtimeStatus } from '@/lib/realtime/context'
import { formatTime } from '@/lib/format'
import { cn } from '@/lib/utils'

const LABEL: Record<string, string> = {
  idle: 'Tempo real inativo',
  connecting: 'Conectando…',
  open: 'Tempo real',
  reconnecting: 'Reconectando…',
  unavailable: 'Tempo real indisponível',
  closed: 'Tempo real desconectado',
}

export function LiveIndicator({ className }: { className?: string }) {
  const { status, lastSyncAt } = useRealtimeStatus()
  const live = status === 'open'
  const warn = status === 'reconnecting' || status === 'connecting'
  return (
    <p role="status" aria-live="polite" className={cn('m-0 flex items-center gap-2 text-xs text-text-muted', className)}>
      <span
        aria-hidden="true"
        className={cn(
          'size-2 shrink-0 rounded-full',
          live ? 'bg-[rgb(var(--success))]' : warn ? 'bg-[rgb(var(--warning))]' : 'bg-[rgb(var(--neutral))]',
        )}
      />
      <span className="whitespace-nowrap">
        {LABEL[status] ?? status}
        {lastSyncAt ? (
          <>
            {' · '}Atualizado às <time dateTime={lastSyncAt.toISOString()}>{formatTime(lastSyncAt)}</time>
          </>
        ) : null}
      </span>
    </p>
  )
}
