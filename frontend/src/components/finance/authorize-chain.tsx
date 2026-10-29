/**
 * Aprovação ÚNICA: criar → autorizar no MESMO diálogo (SPEC AÇÃO 003 §11.8).
 * Os formulários que criam pedidos (despesa, conta a pagar, ajuste, configuração financeira, conciliação,
 * retirada da RD...) chamam `chain.handleCreated(pedido)` depois de criar. Se o pedido é do modo ÚNICA e
 * está pendente, o conteúdo do diálogo troca para a autorização (TOTP + senha financeira + código por e-mail);
 * na DUPLA nada muda (o pedido segue para o outro Master). Uso:
 *
 *   const chain = useAuthorizationChain(() => onOpenChange(false))
 *   ...criou: if (chain.handleCreated(r)) return
 *   <DialogContent>{chain.step ?? (<>…formulário…</>)}</DialogContent>
 */
import * as React from 'react'
import { toast } from 'sonner'
import { AuthorizeStep } from '@/components/finance/authorize-step'
import { needsSelfAuthorization } from '@/components/finance/approval-utils'
import type { ApprovalRequest } from '@/pages/finance/api'

export function useAuthorizationChain(onClose: () => void) {
  const [request, setRequest] = React.useState<ApprovalRequest | null>(null)

  const handleCreated = React.useCallback((r: ApprovalRequest): boolean => {
    if (!needsSelfAuthorization(r)) return false
    setRequest(r)
    return true
  }, [])
  const reset = React.useCallback(() => setRequest(null), [])

  const step = request ? (
    <AuthorizeStep
      request={request}
      onAuthorized={() => {
        setRequest(null)
        onClose()
      }}
      onLater={() => {
        toast.message('Pedido criado. Autorize quando quiser em Financeiro → Aprovações.')
        setRequest(null)
        onClose()
      }}
    />
  ) : null

  return { request, handleCreated, reset, step }
}
