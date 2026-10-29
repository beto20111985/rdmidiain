/** Aviso fixo: o executor real (Portainer) é dependência externa; simulação é rotulada. */
import { AlertTriangle } from 'lucide-react'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'

export function ExternalExecutorNotice({ detail }: { detail?: string }) {
  return (
    <Alert variant="warning">
      <AlertTriangle aria-hidden="true" />
      <div className="flex flex-col gap-1">
        <AlertTitle>Execução real do deploy é dependência externa (Portainer)</AlertTitle>
        <AlertDescription>
          {detail ??
            'Rollouts usam o executor NULO (o alvo fica “Bloqueado — dependência externa”) ou a SIMULAÇÃO LOCAL, sempre rotulada.'}{' '}
          A versão atual de cada instância vem somente do heartbeat dela; uma simulação nunca altera essa versão.
        </AlertDescription>
      </div>
    </Alert>
  )
}
