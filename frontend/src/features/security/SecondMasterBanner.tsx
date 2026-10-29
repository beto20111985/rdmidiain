/**
 * Banner persistente do 2º Administrador Master (SPEC AÇÃO 003 §11.4). Aparece SOMENTE na aprovação
 * DUPLA, para o titular de assento, enquanto o 2º Master não estiver ativo. Na aprovação ÚNICA ele
 * nunca é renderizado (nem texto escondido). Some quando `second_master` vira DONE (evento de tempo
 * real `security.status_changed` recarrega `/me`).
 */
import { Link, useLocation } from 'react-router-dom'
import { ShieldAlert } from 'lucide-react'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { useAuthStore } from '@/stores/authStore'
import { secondMasterPending } from '@/features/auth/setup'
import { SECURITY_SETTINGS_PATH } from '@/components/finance/approval-error-utils'
import { useMastersInfo } from './hooks'

export function SecondMasterBanner() {
  const setup = useAuthStore((s) => s.me?.security_setup)
  const { pathname } = useLocation()
  const pending = secondMasterPending(setup)
  const status = setup?.steps.second_master
  // O e-mail mascarado do convidado vem de /security/masters (só quando há convite pendente).
  const masters = useMastersInfo(pending && status === 'INVITE_PENDING')
  // Na própria tela de Segurança da instância o estado e o convite já estão à vista (o atalho levaria à mesma página).
  if (!pending || pathname === SECURITY_SETTINGS_PATH) return null

  if (status === 'INVITE_PENDING') {
    const email = masters.data?.invite?.email_masked
    return (
      <Alert variant="info" data-testid="second-master-banner" className="mb-4 flex-wrap items-center">
        <ShieldAlert aria-hidden="true" />
        <AlertDescription className="min-w-[14rem] flex-1 text-text">
          {email ? `Convite enviado para ${email} — aguardando ativação` : 'Convite enviado — aguardando ativação'}
        </AlertDescription>
        <Button asChild variant="secondary" className="shrink-0">
          <Link to={SECURITY_SETTINGS_PATH}>Gerenciar</Link>
        </Button>
      </Alert>
    )
  }

  if (status === 'OTHER_MASTER_SETUP_PENDING') {
    return (
      <Alert variant="warning" data-testid="second-master-banner" className="mb-4 flex-wrap items-center">
        <ShieldAlert aria-hidden="true" />
        <AlertDescription className="min-w-[14rem] flex-1 text-text">
          O segundo Administrador Master ainda não concluiu a configuração de segurança.
        </AlertDescription>
        <Button asChild variant="secondary" className="shrink-0">
          <Link to={SECURITY_SETTINGS_PATH}>Ver detalhes</Link>
        </Button>
      </Alert>
    )
  }

  return (
    <Alert variant="warning" data-testid="second-master-banner" className="mb-4 flex-wrap items-center">
      <ShieldAlert aria-hidden="true" />
      <div className="flex min-w-[14rem] flex-1 flex-col gap-0.5">
        <AlertTitle>Configuração de segurança pendente</AlertTitle>
        <AlertDescription className="text-text">
          Cadastre o segundo Administrador Master para liberar operações financeiras que exigem dupla aprovação.
        </AlertDescription>
      </div>
      <Button asChild className="shrink-0">
        <Link to={SECURITY_SETTINGS_PATH}>Configurar agora</Link>
      </Button>
    </Alert>
  )
}
