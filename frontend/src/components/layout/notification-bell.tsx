/**
 * Sino de notificações (A3 #32: o contador fixo "3" foi removido).
 * Contagem REAL de `GET /notifications/unread-count` (contrato v1 §3), atualizada pelo
 * evento WS `notification.created` (domínio 'notifications'). Se a rota não existir ou
 * falhar, o sino aparece SEM contador — nunca um número inventado.
 */
import { Link } from 'react-router-dom'
import { Bell } from 'lucide-react'
import { useUnreadNotifications } from '@/hooks/useUnreadNotifications'
import { buttonVariants } from '@/components/ui/button'
import { cn } from '@/lib/utils'

export function NotificationBell() {
  const { data, isSuccess } = useUnreadNotifications()
  const count = isSuccess && typeof data?.count === 'number' ? data.count : null
  const label = count === null ? 'Notificações' : count === 0 ? 'Notificações, nenhuma não lida' : `Notificações, ${count} não lidas`
  return (
    <Link to="/notificacoes" aria-label={label} className={cn(buttonVariants({ variant: 'ghost', size: 'icon' }), 'relative')}>
      <Bell aria-hidden="true" />
      {count ? (
        <span
          aria-hidden="true"
          className="absolute right-1.5 top-1.5 inline-flex min-w-5 items-center justify-center rounded-full bg-[rgb(var(--danger))] px-1 text-[12px] font-bold leading-5 text-[rgb(var(--bg))] tabular-nums"
        >
          {count > 99 ? '99+' : count}
        </span>
      ) : null}
    </Link>
  )
}
