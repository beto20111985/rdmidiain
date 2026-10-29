/**
 * Configurações → Segurança da instância (`/configuracoes/seguranca`; SPEC AÇÃO 003 §11.7).
 * - Cartão "Aprovação financeira: Única | Dupla" (sem switch; mudança por diálogo guiado e aprovação).
 * - Assentos de Administrador Master + gestão do convite do 2º Master (SÓ na aprovação dupla e só para Master).
 * Estados: carregando, erro (com tentar novamente), sem permissão (403) e instância não inicializada (503).
 * Tempo real: `security.status_changed` invalida as queries de `/security/*` (sem polling).
 */
import { Link } from 'react-router-dom'
import { ShieldCheck } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { ErrorState, NoPermission } from '@/components/patterns/states'
import { isApiError } from '@/lib/errors'
import { useAuthStore } from '@/stores/authStore'
import { securityErrorMessage } from '@/features/auth/authErrors'
import { ApprovalModeCard } from '@/features/security/ApprovalModeCard'
import { MastersList } from '@/features/security/MastersList'
import { useApprovalModeInfo } from '@/features/security/hooks'

export default function SecuritySettingsPage() {
  const isMaster = useAuthStore((s) => !!s.me?.user.is_admin_master)
  const info = useApprovalModeInfo()
  const error = info.error

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Segurança da instância"
        description="Modo de aprovação financeira e Administradores Master."
        breadcrumbs={[{ label: 'Configurações', to: '/configuracoes' }, { label: 'Segurança da instância' }]}
      />

      {info.isLoading ? (
        <Card aria-busy="true">
          <div role="status" className="flex flex-col gap-3">
            <span className="sr-only">Carregando a segurança da instância…</span>
            <Skeleton className="h-6 w-56" />
            <Skeleton className="h-4 w-full max-w-prose" />
            <Skeleton className="h-11 w-64" />
          </div>
        </Card>
      ) : error ? (
        isApiError(error) && error.status === 403 ? (
          <NoPermission
            permission="settings.view"
            action={
              <Button asChild variant="secondary">
                <Link to="/">Ir para o início</Link>
              </Button>
            }
          />
        ) : (
          <ErrorState
            error={error}
            title={isApiError(error) && error.code === 'INSTANCE_NOT_INITIALIZED' ? 'Instância não inicializada' : 'Não foi possível carregar a segurança da instância'}
            description={securityErrorMessage(error)}
            onRetry={() => void info.refetch()}
            retrying={info.isFetching}
          />
        )
      ) : info.data ? (
        <>
          <ApprovalModeCard info={info.data} isMaster={isMaster} />
          {isMaster && info.data.mode === 'DUAL' ? <MastersList /> : null}
          {isMaster && info.data.mode === 'SINGLE' ? (
            <p className="m-0 flex items-start gap-2 text-sm text-text-muted">
              <ShieldCheck aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
              Toda autorização financeira exige o seu código do autenticador, a sua senha financeira e um código enviado por e-mail.
            </p>
          ) : null}
        </>
      ) : null}
    </div>
  )
}
