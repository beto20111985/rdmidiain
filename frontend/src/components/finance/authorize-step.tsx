/**
 * Etapa de autorização (aprovação ÚNICA) exibida DENTRO do diálogo que criou o pedido.
 * Usada por `useAuthorizationChain` (authorize-chain.tsx).
 */
import { DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { DetailList } from '@/components/finance/detail-list'
import { RequestFactorsForm } from '@/components/finance/request-factors-form'
import { useFinanceReady } from '@/components/finance/security-status'
import { formatCents, EMPTY_VALUE } from '@/lib/format'
import type { ApprovalRequest } from '@/pages/finance/api'

export function AuthorizeStep({ request, onAuthorized, onLater }: { request: ApprovalRequest; onAuthorized: () => void; onLater: () => void }) {
  const { ready } = useFinanceReady(true)
  const label = request.action_label ? request.action_label.charAt(0).toUpperCase() + request.action_label.slice(1) : 'Operação'
  return (
    <>
      <DialogHeader>
        <DialogTitle>Autorizar operação</DialogTitle>
        <DialogDescription>
          Pedido criado. Aprovação única: confirme com o código do autenticador, a senha financeira e o código enviado por e-mail para executar.
        </DialogDescription>
      </DialogHeader>
      <DetailList
        columns={1}
        items={[
          { label: 'Operação', value: label },
          ...(request.amount_cents !== null && request.amount_cents !== undefined ? [{ label: 'Valor', value: formatCents(request.amount_cents), numeric: true }] : []),
          { label: 'Motivo', value: request.reason || EMPTY_VALUE },
        ]}
      />
      <RequestFactorsForm request={request} kind="authorize" ready={ready} showTitle={false} onDone={onAuthorized} />
      <DialogFooter>
        <Button variant="secondary" onClick={onLater}>
          Autorizar depois
        </Button>
      </DialogFooter>
    </>
  )
}
