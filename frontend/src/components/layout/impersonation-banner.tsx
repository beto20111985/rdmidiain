/**
 * Banner fixo de impersonation (P001 §96, contrato v1 §2.1): visível em todas as páginas
 * enquanto `impersonation.active`, não dispensável, com botão "Encerrar acesso".
 */
import * as React from 'react'
import { UserRoundCog } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { useAuthStore } from '@/stores/authStore'
import { errorMessage } from '@/lib/errors'

export const BANNER_HEIGHT = 52

export function ImpersonationBanner() {
  const me = useAuthStore((s) => s.me)
  const endImpersonation = useAuthStore((s) => s.endImpersonation)
  const [pending, setPending] = React.useState(false)
  if (!me?.impersonation?.active) return null
  const realName = me.impersonation.real_user?.name
  return (
    <div
      role="region"
      aria-label="Acesso como outro usuário"
      className="sticky top-0 z-banner flex min-h-[52px] flex-wrap items-center gap-x-4 gap-y-2 border-b border-warning bg-[rgb(var(--warning))] px-4 py-2 text-sm font-semibold text-[rgb(var(--bg))] md:px-6"
    >
      <UserRoundCog aria-hidden="true" className="size-5 shrink-0" />
      <p className="m-0 flex-1">
        Você está acessando como <strong className="font-extrabold">{me.user.name}</strong>
        {realName ? ` (usuário real: ${realName})` : ''}. Todas as ações são registradas na auditoria.
      </p>
      <Button
        variant="secondary"
        className="border-[rgb(var(--bg))] bg-[rgb(var(--bg))] text-[rgb(var(--text))] hover:bg-[rgb(var(--bg)/0.85)]"
        loading={pending}
        onClick={async () => {
          setPending(true)
          try {
            await endImpersonation()
            toast.success('Acesso como outro usuário encerrado.')
          } catch (e) {
            toast.error(errorMessage(e))
          } finally {
            setPending(false)
          }
        }}
      >
        Encerrar acesso
      </Button>
    </div>
  )
}
