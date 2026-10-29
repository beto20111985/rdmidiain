/**
 * Erro de uma operação que cria/decide um pedido de aprovação financeira. Mostra a mensagem do
 * servidor (pt-BR) e, no 409 `SECOND_MASTER_REQUIRED` (dupla aprovação com o 2º Master ainda não
 * ativo), o atalho "Configurar agora" — só para quem é Master e pode resolver.
 */
import { Link } from 'react-router-dom'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { SECURITY_SETTINGS_PATH, approvalErrorMessage, isSecondMasterRequired } from './approval-error-utils'
import { useAuthStore } from '@/stores/authStore'
import { cn } from '@/lib/utils'

export function ApprovalErrorAlert({ error, className }: { error: unknown; className?: string }) {
  const isMaster = useAuthStore((s) => !!s.me?.user.is_admin_master)
  if (error === null || error === undefined || error === '') return null
  const secondMaster = isSecondMasterRequired(error)
  return (
    <Alert variant="danger" className={cn(secondMaster && 'items-start', className)}>
      <div className="flex min-w-0 flex-1 flex-col gap-3">
        <AlertDescription className="text-text">{approvalErrorMessage(error)}</AlertDescription>
        {secondMaster && isMaster ? (
          <Button asChild variant="secondary" className="self-start">
            <Link to={SECURITY_SETTINGS_PATH}>Configurar agora</Link>
          </Button>
        ) : null}
      </div>
    </Alert>
  )
}
