/**
 * Status em tempo real: presença (W-PLAY, evento screen.status), dispositivo ativo e telemetria
 * controlada (CPU/RAM/disco/uptime/SSID/sinal — sem IP). Revogar dispositivo com motivo.
 */
import * as React from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Link2Off, QrCode } from 'lucide-react'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Progress } from '@/components/ui/progress'
import { Skeleton } from '@/components/ui/skeleton'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { ErrorState } from '@/components/patterns/states'
import { Can } from '@/components/auth/guards'
import { DetailList } from '@/components/ops/detail-list'
import { PresenceBadge } from '@/components/ops/presence-badge'
import { downloadPercent, lastContactOf } from '@/components/ops/presence-utils'
import { ReasonDialog } from '@/components/ops/reason-dialog'
import { EMPTY_VALUE, formatBytes, formatDateTime, formatDuration, formatNumber } from '@/lib/format'
import { screensApi, presenceToSummary } from '@/pages/ops/api/client'
import { useScreenPresence } from '@/pages/ops/api/hooks'
import type { Screen, Telemetry } from '@/pages/ops/api/types'

function pct(used?: number | null, total?: number | null) {
  if (used == null || !total) return null
  return Math.round((used / total) * 100)
}

function TelemetryList({ t, at, tz }: { t: Telemetry | null; at: string | null; tz?: string }) {
  if (!t) return <p className="m-0 text-sm text-text-muted">Sem telemetria recebida.</p>
  const ram = pct(t.ram_used_mb, t.ram_total_mb)
  const diskUsed = t.disk_total_mb != null && t.disk_free_mb != null ? t.disk_total_mb - t.disk_free_mb : null
  const disk = pct(diskUsed, t.disk_total_mb)
  return (
    <div className="flex flex-col gap-4">
      <DetailList
        columns={2}
        items={[
          { label: 'CPU', value: t.cpu_pct != null ? `${formatNumber(t.cpu_pct, { maximumFractionDigits: 0 })}%` : null, numeric: true },
          {
            label: 'Memória',
            value: ram !== null ? `${ram}% (${formatNumber(t.ram_used_mb)} de ${formatNumber(t.ram_total_mb)} MB)` : null,
            numeric: true,
          },
          {
            label: 'Disco',
            value:
              disk !== null
                ? `${disk}% usado · livre ${formatBytes((t.disk_free_mb ?? 0) * 1024 * 1024)}`
                : null,
            numeric: true,
          },
          { label: 'Tempo ligado', value: t.uptime_s != null ? formatDuration(t.uptime_s) : null, numeric: true },
          { label: 'Rede Wi-Fi (SSID)', value: t.wifi?.ssid ?? null },
          { label: 'Sinal', value: t.wifi?.rssi_dbm != null ? `${t.wifi.rssi_dbm} dBm` : null, numeric: true },
          { label: 'HDMI conectado', value: t.hdmi_connected == null ? null : t.hdmi_connected ? 'Sim' : 'Não' },
          { label: 'Sistema', value: t.os ?? null },
          { label: 'Resolução informada', value: t.resolution ?? null },
          { label: 'Orientação informada', value: t.orientation ?? null },
        ]}
      />
      <p className="m-0 text-xs text-text-muted">
        Telemetria de {formatDateTime(at, tz)}. O painel não coleta endereço IP dos players.
      </p>
    </div>
  )
}

export function ScreenStatusTab({ screen }: { screen: Screen }) {
  const tz = screen.establishment?.timezone
  const qc = useQueryClient()
  const p = useScreenPresence(screen.id)
  const [revoking, setRevoking] = React.useState(false)
  const revoke = useMutation({
    mutationFn: (reason: string) => screensApi.revokeDevice(p.data!.device!.id, reason),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['presence'] })
      void qc.invalidateQueries({ queryKey: ['screens'] })
      toast.success('Dispositivo revogado. O player foi desconectado.')
    },
  })

  if (p.isLoading) return <Skeleton className="h-64 w-full rounded-xl" />
  if (p.error) return <ErrorState error={p.error} onRetry={() => void p.refetch()} retrying={p.isFetching} />
  const data = p.data!
  const summary = presenceToSummary(data)
  const live = data.live
  const dev = data.device
  const dl = downloadPercent(summary)
  const last = lastContactOf(summary)

  return (
    <div className="grid grid-cols-1 gap-6 xl:grid-cols-12">
      <Card className="xl:col-span-7">
        <CardHeader>
          <CardTitle>Presença do player</CardTitle>
          <PresenceBadge presence={summary} timeZone={tz} />
        </CardHeader>
        {!data.paired ? (
          <Alert variant="info">
            <AlertTitle>Nenhum player pareado</AlertTitle>
            <AlertDescription>
              Abra o player na TV, anote o código XXXX-XXXX (ou leia o QR Code) e pareie esta tela.
            </AlertDescription>
          </Alert>
        ) : (
          <DetailList
            items={[
              { label: 'Último contato', value: last ? formatDateTime(last, tz) : null },
              { label: 'Estado informado pelo player', value: live.player_state ?? null },
              { label: 'Programação aplicada', value: live.manifest_version_applied != null ? `v${live.manifest_version_applied}` : null },
              {
                label: 'Programação pendente',
                value: live.manifest_version_pending != null ? `v${live.manifest_version_pending} (baixando)` : 'Nenhuma',
              },
              {
                label: 'Download da programação',
                value:
                  dl !== null ? (
                    <span className="flex items-center gap-2">
                      <Progress value={dl} className="h-2 w-32" aria-label="Download da programação" />
                      <span className="tabular-nums">{dl}%</span>
                    </span>
                  ) : null,
              },
              { label: 'Última sincronização', value: live.last_sync_ms ? formatDateTime(live.last_sync_ms, tz) : null },
              { label: 'Última reprodução registrada', value: live.last_play_ms ? formatDateTime(live.last_play_ms, tz) : null },
              { label: 'Em exibição agora (código RD)', value: live.current_code ?? null },
              {
                label: 'Diferença de relógio',
                value: live.clock_offset_ms != null ? `${formatNumber(live.clock_offset_ms / 1000, { maximumFractionDigits: 1 })} s` : null,
              },
            ]}
          />
        )}
        <CardDescription>
          “Online”/“Conectado” indica comunicação; a reprodução só é comprovada pelos eventos de exibição (proof-of-play).
        </CardDescription>
      </Card>

      <Card className="xl:col-span-5">
        <CardHeader>
          <CardTitle>Dispositivo ativo</CardTitle>
          {dev ? (
            <Can permission="screens.manage">
              <Button variant="destructive" onClick={() => setRevoking(true)}>
                <Link2Off aria-hidden="true" />
                Revogar
              </Button>
            </Can>
          ) : (
            <Can permission="screens.pair">
              {screen.status !== 'DECOMMISSIONED' ? (
                <Button asChild variant="secondary">
                  <Link to={`/telas/parear?screen_id=${screen.id}`}>
                    <QrCode aria-hidden="true" />
                    Parear
                  </Link>
                </Button>
              ) : null}
            </Can>
          )}
        </CardHeader>
        {dev ? (
          <DetailList
            columns={1}
            items={[
              { label: 'Plataforma', value: dev.platform },
              { label: 'Versão do app', value: dev.app_version ?? live.app_version ?? null },
              { label: 'Sistema', value: dev.os_version },
              { label: 'Pareado em', value: formatDateTime(dev.paired_at, tz) },
              { label: 'Método de pareamento', value: dev.pairing_method },
              { label: 'Grupo de atualização', value: dev.update_group },
              { label: 'Impressão da chave', value: dev.key_fingerprint ? `${dev.key_fingerprint.slice(0, 16)}…` : null },
              { label: 'Último erro', value: dev.last_error ?? null },
            ]}
          />
        ) : (
          <p className="m-0 text-sm text-text-muted">{EMPTY_VALUE} Nenhum dispositivo ativo.</p>
        )}
      </Card>

      <Card className="xl:col-span-12">
        <CardHeader>
          <CardTitle>Telemetria</CardTitle>
        </CardHeader>
        <TelemetryList t={dev?.telemetry ?? null} at={dev?.telemetry_at ?? null} tz={tz} />
      </Card>

      <ReasonDialog
        open={revoking}
        onOpenChange={setRevoking}
        title={`Revogar o dispositivo da tela ${screen.friendly_id}?`}
        description="Use em caso de roubo, troca de TV Box ou suspeita de clonagem."
        consequences={[
          'O player é desconectado imediatamente e para de receber programação.',
          'Para voltar a exibir, será preciso parear um dispositivo novamente.',
        ]}
        confirmLabel="Revogar dispositivo"
        destructive
        minReason={3}
        requireText={screen.friendly_id}
        onConfirm={(reason) => revoke.mutateAsync(reason)}
      />
    </div>
  )
}
