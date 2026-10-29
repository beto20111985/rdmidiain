/**
 * Colunas compartilhadas das listas de telas (RD e "Minhas telas"): código TEL-UF-XXXX,
 * estabelecimento, status de cadastro, INVENTÁRIO (vendável ou fora, com o motivo — listas da RD),
 * PRESENÇA em tempo real (com último contato), versão do player, versão de manifesto aplicada/pendente
 * e % de download.
 */
import { Link } from 'react-router-dom'
import { Badge } from '@/components/ui/badge'
import { Progress } from '@/components/ui/progress'
import { PresenceBadge } from '@/components/ops/presence-badge'
import { downloadPercent } from '@/components/ops/presence-utils'
import type { DataTableColumn } from '@/components/patterns/data-table'
import { EMPTY_VALUE } from '@/lib/format'
import { ORIENTATION_LABEL, SCREEN_STATUS_LABEL, labelOf } from '@/pages/ops/api/labels'
import type { PresenceSummary, Screen } from '@/pages/ops/api/types'
import { screenInventoryReason } from './inventoryStatus'

export function screenStatusTone(s: Screen['status']): 'success' | 'warning' | 'neutral' | 'danger' {
  return s === 'ACTIVE' ? 'success' : s === 'MAINTENANCE' ? 'warning' : s === 'DECOMMISSIONED' ? 'danger' : 'neutral'
}

export function screenColumns(opts: {
  presence: Record<string, PresenceSummary> | undefined
  presenceLoading: boolean
  basePath: string
  showEstablishment: boolean
  establishmentLink?: boolean
  /** Coluna "Inventário" (vendável/fora + motivo): "Ativa" é cadastro, não elegibilidade (AÇÃO 004). */
  showInventory?: boolean
}): DataTableColumn<Screen>[] {
  const pres = (s: Screen) => opts.presence?.[s.id]
  const cols: DataTableColumn<Screen>[] = [
    {
      id: 'code',
      header: 'Tela',
      mobile: 'primary',
      cell: (s) => (
        <div className="flex min-w-0 flex-col">
          <Link to={`${opts.basePath}/${s.id}`} className="font-semibold tabular-nums text-link underline-offset-4 hover:underline">
            {s.friendly_id}
          </Link>
          <span className="truncate text-xs text-text-muted">{s.name}</span>
        </div>
      ),
    },
  ]
  if (opts.showEstablishment) {
    cols.push({
      id: 'establishment',
      header: 'Estabelecimento',
      mobile: 'secondary',
      cell: (s) =>
        s.establishment ? (
          <span>
            {opts.establishmentLink ? (
              <Link to={`/estabelecimentos/${s.establishment.id}`} className="text-link underline-offset-4 hover:underline">
                {s.establishment.name}
              </Link>
            ) : (
              s.establishment.name
            )}
            <span className="block text-xs text-text-muted">
              {s.establishment.city}/{s.establishment.state}
            </span>
          </span>
        ) : (
          EMPTY_VALUE
        ),
    })
  }
  cols.push(
    {
      id: 'presence',
      header: 'Presença',
      mobile: 'badge',
      cell: (s) =>
        s.status === 'DECOMMISSIONED' ? (
          <span className="text-text-muted">{EMPTY_VALUE}</span>
        ) : (
          <PresenceBadge presence={pres(s)} loading={opts.presenceLoading && !pres(s)} timeZone={s.establishment?.timezone} />
        ),
    },
    {
      id: 'status',
      header: 'Cadastro',
      cell: (s) => <Badge tone={screenStatusTone(s.status)}>{labelOf(SCREEN_STATUS_LABEL, s.status)}</Badge>,
    },
    ...(opts.showInventory
      ? [
          {
            id: 'inventory',
            header: 'Inventário',
            cell: (s: Screen) => {
              if (s.status === 'DECOMMISSIONED') return <span className="text-text-muted">{EMPTY_VALUE}</span>
              if (s.sellable) return <Badge tone="success">Vendável</Badge>
              const why = screenInventoryReason(s)
              return (
                <span className="flex flex-col items-start gap-1">
                  <Badge tone="neutral">Fora do inventário</Badge>
                  {why ? <span className="text-xs text-text-muted">{why.reason}</span> : null}
                </span>
              )
            },
          } satisfies DataTableColumn<Screen>,
        ]
      : []),
    {
      id: 'player',
      header: 'Versão do player',
      cell: (s) => <span className="tabular-nums">{pres(s)?.app_version ?? EMPTY_VALUE}</span>,
    },
    {
      id: 'manifest',
      header: 'Programação aplicada',
      cell: (s) => {
        const p = pres(s)
        if (!p || p.manifest_version_applied == null) return <span className="text-text-muted">{EMPTY_VALUE}</span>
        return (
          <span className="tabular-nums">
            v{p.manifest_version_applied}
            {p.manifest_version_pending != null ? (
              <span className="block text-xs text-warning">v{p.manifest_version_pending} pendente</span>
            ) : null}
          </span>
        )
      },
    },
    {
      id: 'download',
      header: 'Download',
      numeric: true,
      cell: (s) => {
        const p = pres(s)
        const pct = downloadPercent(p)
        if (pct === null || p?.manifest_version_pending == null) return <span className="text-text-muted">{EMPTY_VALUE}</span>
        return (
          <div className="flex min-w-24 flex-col items-end gap-1">
            <span className="tabular-nums">{pct}%</span>
            <Progress value={pct} aria-label={`Download da programação de ${s.friendly_id}`} className="h-1.5 w-24" />
          </div>
        )
      },
    },
    {
      id: 'orientation',
      header: 'Orientação',
      mobile: 'hidden',
      cell: (s) => labelOf(ORIENTATION_LABEL, s.orientation),
    },
  )
  return cols
}
