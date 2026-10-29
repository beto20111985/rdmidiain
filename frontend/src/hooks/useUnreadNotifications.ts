import { useQuery } from '@tanstack/react-query'
import { http } from '@/lib/http'
import { qk } from '@/lib/queryKeys'
import { useAuthStore } from '@/stores/authStore'

/**
 * Contagem de notificações não lidas — `GET /notifications/unread-count` → {"count": n}
 * (contrato v1 §3). Sem polling: o evento WS `notification.created` invalida o domínio
 * 'notifications'. Erro/404 → sem contador (nunca número inventado).
 */
export function useUnreadNotifications() {
  const enabled = useAuthStore((s) => s.status === 'authenticated' && !s.demo)
  return useQuery({
    queryKey: qk.notificationsUnread,
    queryFn: () => http.get<{ count: number }>('/notifications/unread-count'),
    enabled,
    staleTime: Infinity,
    retry: false,
  })
}
