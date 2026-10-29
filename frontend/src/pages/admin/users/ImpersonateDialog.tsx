/**
 * Acesso como outro usuário (impersonation — contrato §2.1): motivo OBRIGATÓRIO (≥ 10
 * caracteres), auditado no início/fim e em cada ação. Proibido para administradores e
 * Admin Master (o servidor bloqueia: 403 IMPERSONATION_FORBIDDEN_TARGET). O token de
 * impersonation fica só em memória; o banner fixo da fundação aparece enquanto ativo.
 */
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { http, tokenStore } from '@/lib/http'
import { useAuthStore } from '@/stores/authStore'
import { ReasonDialog } from '@/components/admin/ui'

interface ImpersonationResponse {
  access_token: string
  expires_in: number
  impersonation: { active: boolean; user: { id: string; name?: string } | null; reason: string }
}

export function ImpersonateDialog({
  user,
  open,
  onOpenChange,
}: {
  user: { id: string; name: string }
  open: boolean
  onOpenChange: (o: boolean) => void
}) {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const reloadMe = useAuthStore((s) => s.reloadMe)
  return (
    <ReasonDialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Acessar como ${user.name}?`}
      description="Você verá o painel exatamente como esta pessoa. O acesso expira em 15 minutos ou quando você clicar em “Encerrar acesso”."
      consequences={[
        'O motivo, o início, o fim e todas as ações feitas durante o acesso ficam registrados na auditoria com o seu usuário real.',
        'Ações sensíveis (senha, 2FA, dados financeiros, exportações) continuam bloqueadas durante o acesso.',
      ]}
      reasonLabel="Motivo do acesso"
      reasonHint="Ex.: chamado CH-ABC123 — cliente não encontra a campanha na lista."
      minLength={10}
      confirmLabel="Iniciar acesso"
      onConfirm={async (reason) => {
        const res = await http.post<ImpersonationResponse>('/auth/impersonation', { user_id: user.id, reason })
        tokenStore.set(res.access_token)
        qc.removeQueries({ predicate: (q) => q.queryKey[0] !== 'me' })
        await reloadMe()
        toast.success(`Você está acessando como ${user.name}.`)
        navigate('/', { replace: true })
      }}
    />
  )
}
