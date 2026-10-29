/**
 * Comandos remotos (W-PLAY): restart_app, resync, clear_cache, redownload, update_app, reboot,
 * reload_manifest — sempre com motivo (auditoria) e confirmação; reboot/clear_cache/update_app exigem
 * confirmação forte (código da tela digitado; backend exige confirm=true). PIN técnico por rota própria.
 * Histórico com status de entrega/ack. Idempotency-Key em cada envio.
 */
import * as React from 'react'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { KeyRound, Terminal } from 'lucide-react'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { EmptyState, ErrorState } from '@/components/patterns/states'
import { ReasonDialog } from '@/components/ops/reason-dialog'
import { TechPinDialog } from '@/components/ops/tech-pin-dialog'
import { UserName } from '@/components/ops/entity-name'
import { useCan } from '@/hooks/usePermissions'
import { qk } from '@/lib/queryKeys'
import { formatDateTime } from '@/lib/format'
import { screensApi } from '@/pages/ops/api/client'
import { COMMAND_HELP, COMMAND_LABEL, COMMAND_STATUS_LABEL, CONFIRM_REQUIRED_COMMANDS, labelOf } from '@/pages/ops/api/labels'
import type { CommandName, Screen } from '@/pages/ops/api/types'

const COMMANDS: CommandName[] = ['resync', 'reload_manifest', 'redownload', 'restart_app', 'update_app', 'clear_cache', 'reboot']

const statusTone = (s: string) =>
  s === 'DONE' ? 'success' : s === 'FAILED' || s === 'UNSUPPORTED' || s === 'EXPIRED' ? 'danger' : s === 'CANCELLED' ? 'neutral' : 'warning'

export function ScreenCommandsTab({ screen, paired }: { screen: Screen; paired: boolean }) {
  const can = useCan()
  const qc = useQueryClient()
  const tz = screen.establishment?.timezone
  const [page, setPage] = React.useState(1)
  const [cmd, setCmd] = React.useState<CommandName | null>(null)
  const [pinOpen, setPinOpen] = React.useState(false)
  const hist = useQuery({
    queryKey: qk.sub('screens', 'commands', screen.id, page),
    queryFn: () => screensApi.commands(screen.id, page),
    placeholderData: keepPreviousData,
  })
  const send = useMutation({
    mutationFn: ({ command, reason }: { command: CommandName; reason: string }) =>
      screensApi.sendCommand(screen.id, { command, reason, confirm: true }),
    onSuccess: (c) => {
      void qc.invalidateQueries({ queryKey: qk.sub('screens', 'commands', screen.id) })
      toast.success(
        c.status === 'PENDING'
          ? `Comando “${labelOf(COMMAND_LABEL, c.command)}” na fila: será entregue quando o player se conectar.`
          : `Comando “${labelOf(COMMAND_LABEL, c.command)}” enviado.`,
      )
    },
  })
  const canCommand = can('screens.command') && screen.status !== 'DECOMMISSIONED'
  const pages = Math.max(1, Math.ceil((hist.data?.total ?? 0) / 10))

  return (
    <div className="grid grid-cols-1 gap-6 xl:grid-cols-12">
      <Card className="xl:col-span-5">
        <CardHeader>
          <CardTitle>Enviar comando</CardTitle>
        </CardHeader>
        {!canCommand ? (
          <p className="m-0 text-sm text-text-muted">
            {screen.status === 'DECOMMISSIONED' ? 'Tela retirada definitivamente.' : 'Seu perfil não envia comandos remotos (screens.command).'}
          </p>
        ) : (
          <>
            {!paired ? (
              <Alert variant="warning">
                <AlertDescription className="text-text">Nenhum player pareado: comandos não terão destino.</AlertDescription>
              </Alert>
            ) : null}
            <ul className="m-0 flex list-none flex-col gap-2 p-0">
              {COMMANDS.map((c) => (
                <li key={c} className="flex flex-col gap-1 rounded-lg border border-border p-3 sm:flex-row sm:items-center sm:justify-between">
                  <div className="flex min-w-0 flex-col">
                    <span className="font-semibold text-text">{COMMAND_LABEL[c]}</span>
                    <span className="text-xs text-text-muted">{COMMAND_HELP[c]}</span>
                  </div>
                  <Button
                    variant={CONFIRM_REQUIRED_COMMANDS.has(c) ? 'destructive' : 'secondary'}
                    onClick={() => setCmd(c)}
                    disabled={!paired}
                    aria-label={`${COMMAND_LABEL[c]} na tela ${screen.friendly_id}`}
                  >
                    <Terminal aria-hidden="true" />
                    Enviar
                  </Button>
                </li>
              ))}
              <li className="flex flex-col gap-1 rounded-lg border border-border p-3 sm:flex-row sm:items-center sm:justify-between">
                <div className="flex min-w-0 flex-col">
                  <span className="font-semibold text-text">Definir PIN técnico</span>
                  <span className="text-xs text-text-muted">PIN do menu técnico/saída do modo quiosque (o servidor guarda só o hash).</span>
                </div>
                <Button variant="secondary" onClick={() => setPinOpen(true)} disabled={!paired} aria-label={`Definir PIN técnico da tela ${screen.friendly_id}`}>
                  <KeyRound aria-hidden="true" />
                  Definir
                </Button>
              </li>
            </ul>
          </>
        )}
      </Card>

      <Card className="xl:col-span-7">
        <CardHeader>
          <CardTitle>Histórico de comandos</CardTitle>
          <CardDescription>Entregue = enviado ao player; Concluído = o player confirmou a execução.</CardDescription>
        </CardHeader>
        {hist.isLoading ? (
          <Skeleton className="h-40 w-full" />
        ) : hist.error && !hist.data ? (
          <ErrorState error={hist.error} onRetry={() => void hist.refetch()} size="compact" />
        ) : !hist.data?.items.length ? (
          <EmptyState icon={Terminal} size="compact" headingLevel="h3" title="Nenhum comando enviado para esta tela." />
        ) : (
          <>
            <ol className="m-0 flex list-none flex-col divide-y divide-border p-0">
              {hist.data.items.map((c) => (
                <li key={c.id} className="flex flex-col gap-1 py-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="font-semibold text-text">{labelOf(COMMAND_LABEL, c.command)}</span>
                    <Badge tone={statusTone(c.status)}>{labelOf(COMMAND_STATUS_LABEL, c.status)}</Badge>
                  </div>
                  <span className="text-xs text-text-muted">
                    {formatDateTime(c.created_at, tz)} · por <UserName id={c.issued_by_user_id} name={c.issued_by_name} />
                    {c.acked_at ? ` · resposta ${formatDateTime(c.acked_at, tz)}` : ''}
                    {c.delivery_count > 1 ? ` · ${c.delivery_count} entregas` : ''}
                  </span>
                  <span className="text-sm text-text">Motivo: {c.reason}</span>
                  {c.result_detail ? <span className="text-xs text-text-muted">Resultado: {c.result_detail}</span> : null}
                </li>
              ))}
            </ol>
            {pages > 1 ? (
              <nav aria-label="Paginação do histórico de comandos" className="flex items-center justify-end gap-2 text-sm text-text-muted">
                <Button variant="secondary" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
                  Anterior
                </Button>
                <span aria-current="page">
                  Página {page} de {pages}
                </span>
                <Button variant="secondary" disabled={page >= pages} onClick={() => setPage((p) => p + 1)}>
                  Próxima
                </Button>
              </nav>
            ) : null}
          </>
        )}
      </Card>

      <ReasonDialog
        open={!!cmd}
        onOpenChange={(o) => !o && setCmd(null)}
        title={cmd ? `${COMMAND_LABEL[cmd]} — ${screen.friendly_id}?` : ''}
        description={cmd ? COMMAND_HELP[cmd] : undefined}
        consequences={
          cmd && CONFIRM_REQUIRED_COMMANDS.has(cmd)
            ? ['Ação com impacto na exibição: confirme digitando o código da tela.', 'O comando fica registrado na auditoria.']
            : ['O comando fica registrado na auditoria. Se o player estiver offline, será entregue na reconexão.']
        }
        confirmLabel="Enviar comando"
        destructive={!!cmd && CONFIRM_REQUIRED_COMMANDS.has(cmd)}
        minReason={3}
        requireText={cmd && CONFIRM_REQUIRED_COMMANDS.has(cmd) ? screen.friendly_id : undefined}
        onConfirm={(reason) => send.mutateAsync({ command: cmd!, reason })}
      />
      <TechPinDialog
        open={pinOpen}
        onOpenChange={setPinOpen}
        targetLabel={`a tela ${screen.friendly_id}`}
        onSubmit={async (pin, reason) => {
          await screensApi.setTechPin(screen.id, { pin, reason })
          void qc.invalidateQueries({ queryKey: qk.sub('screens', 'commands', screen.id) })
          toast.success('PIN técnico enviado ao player.')
        }}
      />
    </div>
  )
}
