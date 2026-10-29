/**
 * Assentos de Administrador Master e gestão do convite do 2º Master (SPEC AÇÃO 003 §5.2/§11.7).
 * SÓ na aprovação DUPLA e só para Master: na ÚNICA esta lista não é montada. Mostra apenas nome e
 * estado do outro Master (nunca e-mail completo, telefone ou fatores) e o e-mail MASCARADO do convite.
 * O link de ativação nunca aparece aqui: vai somente ao e-mail do convidado.
 */
import * as React from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { MailCheck, RefreshCw, ShieldCheck, UserRound } from 'lucide-react'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Skeleton } from '@/components/ui/skeleton'
import { ErrorState } from '@/components/patterns/states'
import { ConfirmDialog } from '@/components/patterns/confirm-dialog'
import { securityErrorMessage } from '@/features/auth/authErrors'
import { formatDateTime } from '@/lib/format'
import { useAuthStore } from '@/stores/authStore'
import { InviteSecondMasterForm } from './InviteSecondMasterForm'
import { securityApi } from './api'
import { useMastersInfo } from './hooks'
import type { MasterInvite, MasterSeat, SeatStatus } from './types'

const SEAT_LABEL: Record<SeatStatus, { label: string; tone: 'success' | 'warning' | 'neutral' }> = {
  ACTIVE: { label: 'Ativo', tone: 'success' },
  SETUP_PENDING: { label: 'Configuração pendente', tone: 'warning' },
  VACANT: { label: 'Vago', tone: 'neutral' },
}

function SeatRow({ seat }: { seat: MasterSeat }) {
  const s = SEAT_LABEL[seat.status] ?? SEAT_LABEL.VACANT
  return (
    <li className="flex flex-wrap items-center justify-between gap-3 py-3">
      <div className="flex min-w-0 items-center gap-3">
        <UserRound aria-hidden="true" className="size-5 shrink-0 text-text-muted" />
        <div className="flex min-w-0 flex-col">
          <span className="text-sm font-semibold text-text">Assento {seat.seat}</span>
          <span className="truncate text-sm text-text-muted">
            {seat.status === 'VACANT' ? 'Nenhum Administrador Master cadastrado' : `${seat.name ?? 'Administrador Master'}${seat.is_self ? ' (você)' : ''}`}
          </span>
        </div>
      </div>
      <Badge tone={s.tone}>{s.label}</Badge>
    </li>
  )
}

const INVITE_NOTE: Record<string, string> = {
  EXPIRED: 'O último convite expirou.',
  LOCKED: 'O último convite foi bloqueado por tentativas inválidas.',
  REVOKED: 'O último convite foi revogado.',
}

function InviteManagement({ invite }: { invite: MasterInvite }) {
  const qc = useQueryClient()
  const reloadMe = useAuthStore((s) => s.reloadMe)
  const [confirmRevoke, setConfirmRevoke] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)
  const refresh = async () => {
    await qc.invalidateQueries({ queryKey: ['security'] })
    await reloadMe().catch(() => undefined)
  }
  const resend = useMutation({
    mutationFn: () => securityApi.resendInvite(invite.id),
    onSuccess: async (r) => {
      toast.success(`Convite reenviado. O link anterior deixou de valer; o novo vale até ${formatDateTime(r.expires_at)}.`)
      setError(null)
      await refresh()
    },
    onError: (e) => setError(securityErrorMessage(e)),
  })
  return (
    <div className="flex flex-col gap-4">
      <Alert variant="info">
        <MailCheck aria-hidden="true" />
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <AlertTitle>Convite pendente</AlertTitle>
          <AlertDescription className="text-text">
            Convite para {invite.full_name} ({invite.email_masked}), enviado {invite.sent_count} {invite.sent_count === 1 ? 'vez' : 'vezes'}
            {invite.invited_by_name ? ` por ${invite.invited_by_name}` : ''}. Vale até{' '}
            <time dateTime={invite.expires_at}>{formatDateTime(invite.expires_at)}</time>. A ativação acontece quando o convidado concluir o
            cadastro; ele cria a própria senha, o próprio autenticador e a própria senha financeira.
          </AlertDescription>
        </div>
      </Alert>
      {error ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">{error}</AlertDescription>
        </Alert>
      ) : null}
      <div className="flex flex-wrap gap-3">
        <Button variant="secondary" onClick={() => resend.mutate()} loading={resend.isPending} loadingText="Reenviando…">
          <RefreshCw aria-hidden="true" />
          Reenviar convite
        </Button>
        <Button variant="destructive" onClick={() => setConfirmRevoke(true)}>
          Revogar convite
        </Button>
      </div>
      <ConfirmDialog
        open={confirmRevoke}
        onOpenChange={setConfirmRevoke}
        destructive
        title="Revogar este convite?"
        description={`O link enviado para ${invite.email_masked} deixa de funcionar.`}
        consequences={['Quem recebeu o convite não consegue mais ativar a conta com esse link.', 'Você poderá enviar um novo convite depois.']}
        confirmLabel="Revogar convite"
        cancelLabel="Voltar"
        onConfirm={async () => {
          await securityApi.revokeInvite(invite.id)
          toast.success('Convite revogado.')
          await refresh()
        }}
      />
    </div>
  )
}

export function MastersList() {
  const q = useMastersInfo()
  const data = q.data
  const activeInvite = data?.invite && data.invite.status === 'PENDING' ? data.invite : null
  const lastNote = data?.invite && data.invite.status !== 'PENDING' ? INVITE_NOTE[data.invite.status] : undefined

  return (
    <Card aria-labelledby="masters-title" id="segundo-master">
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle id="masters-title">Administradores Master</CardTitle>
          <CardDescription>
            Os dois assentos são independentes: cada Master tem a própria senha, o próprio autenticador e a própria senha financeira.
          </CardDescription>
        </div>
      </CardHeader>
      {q.isLoading ? (
        <div role="status" className="flex flex-col gap-3">
          <span className="sr-only">Carregando Administradores Master…</span>
          <Skeleton className="h-14 w-full" />
          <Skeleton className="h-14 w-full" />
        </div>
      ) : q.error ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} size="compact" />
      ) : data ? (
        <>
          <ul aria-label="Assentos de Administrador Master" className="m-0 flex list-none flex-col divide-y divide-border p-0">
            {data.seats.map((s) => (
              <SeatRow key={s.seat} seat={s} />
            ))}
          </ul>
          {activeInvite ? <InviteManagement invite={activeInvite} /> : null}
          {!activeInvite && data.can_invite ? (
            <section aria-labelledby="invite-title" className="flex flex-col gap-4">
              <div className="flex flex-col gap-1">
                <h3 id="invite-title" className="text-title-card text-text">
                  <ShieldCheck aria-hidden="true" className="mr-1.5 inline size-4 text-text-muted" />
                  Convidar o segundo Administrador Master
                </h3>
                {lastNote ? <p className="m-0 text-sm text-warning">{lastNote}</p> : null}
              </div>
              <InviteSecondMasterForm />
            </section>
          ) : null}
        </>
      ) : null}
    </Card>
  )
}
