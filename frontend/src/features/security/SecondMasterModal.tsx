/**
 * Modal do 2º Administrador Master (SPEC AÇÃO 003 §11.3): ao iniciar cada sessão (login/boot), só na
 * aprovação DUPLA, para o titular de assento e com `second_master === 'NOT_INVITED'`. "Fazer depois"
 * vale até o fim da sessão (estado em memória); o banner continua. Na aprovação ÚNICA nunca aparece.
 */
import * as React from 'react'
import { useLocation } from 'react-router-dom'
import { CheckCircle2 } from 'lucide-react'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { useAuthStore } from '@/stores/authStore'
import { secondMasterPending } from '@/features/auth/setup'
import { SECURITY_SETTINGS_PATH } from '@/components/finance/approval-error-utils'
import { InviteSecondMasterForm } from './InviteSecondMasterForm'
import { useSecurityUiStore } from './uiStore'
import type { MasterInvite } from './types'

type View = 'intro' | 'form' | 'done'

export function SecondMasterModal() {
  const setup = useAuthStore((s) => s.me?.security_setup)
  const dismissed = useSecurityUiStore((s) => s.secondMasterModalDismissed)
  const dismiss = useSecurityUiStore((s) => s.dismissSecondMasterModal)
  const { pathname } = useLocation()
  const [view, setView] = React.useState<View>('intro')
  const [invite, setInvite] = React.useState<MasterInvite | null>(null)

  // Na própria tela de segurança o convite já está à vista: o modal seria redundante.
  const onSecurityPage = pathname === SECURITY_SETTINGS_PATH || pathname === '/primeiro-acesso'
  const should = secondMasterPending(setup) && setup?.steps.second_master === 'NOT_INVITED' && !dismissed && !onSecurityPage
  const open = should || view === 'done'
  if (!open) return null

  const close = () => {
    dismiss()
    setView('intro')
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !o && close()}>
      <DialogContent size="md">
        {view === 'done' ? (
          <>
            <DialogHeader>
              <DialogTitle>Convite enviado</DialogTitle>
              <DialogDescription>
                <CheckCircle2 aria-hidden="true" className="mr-1.5 inline size-4 text-success" />
                {invite ? `Enviamos o convite para ${invite.email_masked}.` : 'O convite foi enviado.'} A dupla aprovação será liberada quando o
                convidado concluir a ativação.
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button onClick={close}>Fechar</Button>
            </DialogFooter>
          </>
        ) : view === 'form' ? (
          <>
            <DialogHeader>
              <DialogTitle>Convidar o segundo Administrador Master</DialogTitle>
              <DialogDescription>Informe quem será o segundo Master. Ele receberá um link de uso único por e-mail.</DialogDescription>
            </DialogHeader>
            <InviteSecondMasterForm
              autoFocus
              onInvited={(i) => {
                setInvite(i)
                setView('done')
              }}
              onCancel={close}
              cancelLabel="Fazer depois"
            />
          </>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>Configure o segundo Administrador Master</DialogTitle>
              <DialogDescription>
                Esta instalação utiliza dupla aprovação financeira. Para liberar operações financeiras sensíveis, é necessário cadastrar um
                segundo Administrador Master independente.
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="secondary" onClick={close}>
                Fazer depois
              </Button>
              <Button onClick={() => setView('form')}>Convidar agora</Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
