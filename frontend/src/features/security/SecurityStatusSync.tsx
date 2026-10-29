/**
 * Mantém o estado de segurança do usuário atualizado: o evento de tempo real `security.status_changed`
 * (transição de modo, convite criado/revogado/expirado, ativação do 2º Master, recuperação) recarrega
 * `/me` — que carrega `security_setup` — para o banner, o modal e as telas reagirem sem polling.
 * As queries de `/security/*` são invalidadas pelo mapa de eventos (eventMap.ts).
 */
import { useRealtimeEvent } from '@/lib/realtime/context'
import { useAuthStore } from '@/stores/authStore'

export function SecurityStatusSync() {
  useRealtimeEvent('security.status_changed', () => {
    const { status, demo, reloadMe } = useAuthStore.getState()
    if (demo || status !== 'authenticated') return
    void reloadMe().catch(() => undefined)
  })
  return null
}
