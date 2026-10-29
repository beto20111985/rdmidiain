/**
 * Passo 4 — convidar o 2º Administrador Master. NÃO bloqueante e SÓ existe na aprovação DUPLA para o
 * titular de assento (quem monta o assistente decide; na aprovação ÚNICA este componente nunca é renderizado).
 */
import { CheckCircle2, Info } from 'lucide-react'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { InviteSecondMasterForm } from '@/features/security/InviteSecondMasterForm'
import { useMastersInfo } from '@/features/security/hooks'
import { formatDateTime } from '@/lib/format'
import type { SecondMasterStatus } from '../../types'

export function SecondMasterStep({ status, onFinish }: { status: SecondMasterStatus; onFinish: () => void }) {
  const pending = status === 'INVITE_PENDING'
  const masters = useMastersInfo(pending)

  if (status === 'NOT_INVITED') {
    return (
      <div className="flex flex-col gap-6">
        <p className="m-0 text-sm text-text">
          Esta instalação utiliza dupla aprovação financeira. Para liberar operações financeiras sensíveis, é necessário cadastrar um segundo
          Administrador Master independente.
        </p>
        <InviteSecondMasterForm onCancel={onFinish} cancelLabel="Fazer depois" autoFocus />
      </div>
    )
  }

  if (pending) {
    return (
      <div className="flex flex-col gap-6">
        <Alert variant="success">
          <CheckCircle2 aria-hidden="true" />
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <AlertTitle>Convite enviado</AlertTitle>
            <AlertDescription className="text-text">
              {masters.isLoading ? (
                <Skeleton className="h-5 w-64" />
              ) : masters.data?.invite ? (
                <>
                  Enviamos o convite para {masters.data.invite.email_masked}. Ele vale até{' '}
                  <time dateTime={masters.data.invite.expires_at}>{formatDateTime(masters.data.invite.expires_at)}</time>. A ativação acontece
                  quando o convidado concluir o cadastro; você pode acompanhar ou reenviar em Configurações → Segurança da instância.
                </>
              ) : (
                'O convite foi enviado. Acompanhe a ativação em Configurações → Segurança da instância.'
              )}
            </AlertDescription>
          </div>
        </Alert>
        <Button size="block" onClick={onFinish}>
          Ir para o painel
        </Button>
      </div>
    )
  }

  // OTHER_MASTER_SETUP_PENDING (e DONE, por segurança): nada a convidar.
  return (
    <div className="flex flex-col gap-6">
      <Alert variant="info">
        <Info aria-hidden="true" />
        <AlertDescription className="text-text">
          {status === 'DONE'
            ? 'O segundo Administrador Master já está ativo.'
            : 'O segundo Administrador Master ainda não concluiu a configuração de segurança.'}
        </AlertDescription>
      </Alert>
      <Button size="block" onClick={onFinish}>
        Ir para o painel
      </Button>
    </div>
  )
}
