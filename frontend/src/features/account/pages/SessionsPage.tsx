/**
 * Sessões ativas (E2 #69–71): IP, navegador e dispositivo; encerrar uma sessão
 * (DELETE /auth/sessions/{id}) ou sair de todos os dispositivos (POST /auth/logout-all).
 */
import * as React from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { LogOut, MonitorSmartphone } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Skeleton } from '@/components/ui/skeleton'
import { ConfirmDialog } from '@/components/patterns/confirm-dialog'
import { EmptyState, ErrorState } from '@/components/patterns/states'
import { useIsMobile } from '@/hooks/useMediaQuery'
import { useAuthStore } from '@/stores/authStore'
import { qk } from '@/lib/queryKeys'
import { formatDateTime, EMPTY_VALUE } from '@/lib/format'
import { authApi, sessionsList } from '@/features/auth/api'
import type { SessionInfo } from '@/features/auth/types'

function deviceName(s: SessionInfo) {
  return s.device_label || s.user_agent || 'Dispositivo desconhecido'
}

export default function SessionsPage() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const isMobile = useIsMobile()
  const demo = useAuthStore((s) => s.demo)
  const logoutAll = useAuthStore((s) => s.logoutAll)
  const [revoking, setRevoking] = React.useState<SessionInfo | null>(null)
  const [confirmAll, setConfirmAll] = React.useState(false)

  const query = useQuery({ queryKey: qk.sessions, queryFn: authApi.sessions, enabled: !demo, select: sessionsList })
  const revoke = useMutation({
    mutationFn: (id: string) => authApi.revokeSession(id),
    onSuccess: () => {
      toast.success('Sessão encerrada.')
      void qc.invalidateQueries({ queryKey: qk.sessions })
    },
  })

  const sessions = query.data ?? []
  const actions = (
    <Button variant="destructive" onClick={() => setConfirmAll(true)} disabled={demo || !sessions.length}>
      <LogOut aria-hidden="true" />
      Sair de todos os dispositivos
    </Button>
  )

  let content: React.ReactNode
  if (demo) {
    content = <EmptyState title="Indisponível no modo demonstração" description="As sessões vêm do servidor de autenticação." size="compact" />
  } else if (query.isLoading) {
    content = (
      <div role="status" className="flex flex-col gap-2">
        <span className="sr-only">Carregando sessões…</span>
        {Array.from({ length: 3 }).map((_, i) => (
          <Skeleton key={i} className="h-[52px] w-full" />
        ))}
      </div>
    )
  } else if (query.error) {
    content = <ErrorState error={query.error} onRetry={() => void query.refetch()} retrying={query.isFetching} size="compact" />
  } else if (!sessions.length) {
    content = <EmptyState icon={MonitorSmartphone} title="Nenhuma sessão ativa encontrada" size="compact" />
  } else if (isMobile) {
    content = (
      <ul className="m-0 flex list-none flex-col gap-3 p-0" aria-label="Sessões ativas">
        {sessions.map((s) => (
          <li key={s.id} className="flex flex-col gap-2 rounded-lg border border-border p-4">
            <div className="flex items-start justify-between gap-2">
              <span className="font-semibold text-text">{deviceName(s)}</span>
              {s.current ? <Badge tone="info">Esta sessão</Badge> : null}
            </div>
            <p className="m-0 text-xs text-text-muted">
              IP {s.ip ?? EMPTY_VALUE} · último uso {formatDateTime(s.last_seen_at)}
            </p>
            {!s.current ? (
              <Button variant="secondary" onClick={() => setRevoking(s)}>
                Encerrar sessão
              </Button>
            ) : null}
          </li>
        ))}
      </ul>
    )
  } else {
    content = (
      <Table>
        <caption className="sr-only">Sessões ativas</caption>
        <TableHeader>
          <TableRow className="h-11 hover:bg-transparent">
            <TableHead scope="col">Dispositivo</TableHead>
            <TableHead scope="col">IP</TableHead>
            <TableHead scope="col">Início</TableHead>
            <TableHead scope="col">Último uso</TableHead>
            <TableHead scope="col" className="text-right">
              Ações
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {sessions.map((s) => (
            <TableRow key={s.id}>
              <TableCell>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-semibold">{deviceName(s)}</span>
                  {s.current ? <Badge tone="info">Esta sessão</Badge> : null}
                </div>
              </TableCell>
              <TableCell className="tabular-nums">{s.ip ?? EMPTY_VALUE}</TableCell>
              <TableCell className="tabular-nums">{formatDateTime(s.created_at)}</TableCell>
              <TableCell className="tabular-nums">{formatDateTime(s.last_seen_at)}</TableCell>
              <TableCell className="text-right">
                {!s.current ? (
                  <Button variant="secondary" onClick={() => setRevoking(s)}>
                    Encerrar
                  </Button>
                ) : null}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    )
  }

  return (
    <>
      <PageHeader
        title="Sessões ativas"
        breadcrumbs={[{ label: 'Minha conta', to: '/minha-conta' }, { label: 'Sessões ativas' }]}
        actions={actions}
      />
      <Card>{content}</Card>
      <ConfirmDialog
        open={!!revoking}
        onOpenChange={(o) => !o && setRevoking(null)}
        title="Encerrar esta sessão?"
        description={revoking ? `${deviceName(revoking)} precisará entrar novamente.` : undefined}
        confirmLabel="Encerrar sessão"
        destructive
        onConfirm={async () => {
          if (revoking) await revoke.mutateAsync(revoking.id)
        }}
      />
      <ConfirmDialog
        open={confirmAll}
        onOpenChange={setConfirmAll}
        title="Sair de todos os dispositivos?"
        description="Todas as sessões, inclusive esta, serão encerradas. Você precisará entrar novamente."
        confirmLabel="Sair de todos"
        destructive
        onConfirm={async () => {
          await logoutAll()
          navigate('/login', { replace: true })
        }}
      />
    </>
  )
}
