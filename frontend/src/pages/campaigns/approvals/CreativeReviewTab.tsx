/**
 * Revisão RD de anúncios que não passam por campanha: conteúdo INTERNO, INSTITUCIONAL e de
 * RESERVA (fallback) — `POST /creatives/{id}/review {decision, reason?}` (`campaigns.approve`
 * GLOBAL). Anúncios EXTERNOS (anunciantes) são revisados pela fila de campanhas.
 */
import * as React from 'react'
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { CheckCircle2, Eye, XCircle } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { ConfirmDialog } from '@/components/patterns/confirm-dialog'
import { ErrorState } from '@/components/patterns/states'
import { ReasonDialog } from '@/components/campaigns/reason-dialog'
import { ToneBadge } from '@/components/campaigns/badges'
import { OrgName } from '@/components/campaigns/org-name'
import { MediaPreview } from '@/components/campaigns/media-preview'
import { useTableState } from '@/hooks/useTableState'
import { qk } from '@/lib/queryKeys'
import { formatDateTime } from '@/lib/format'
import { creativesApi, mediaApi } from '../lib/api'
import { CREATIVE_STATUS, TECHNICAL_STATUS } from '../lib/labels'
import { withDescribedError } from '../lib/errorText'
import type { Creative } from '../lib/types'

const KINDS = [
  { value: 'INTERNAL', label: 'Conteúdo interno' },
  { value: 'INSTITUTIONAL', label: 'Institucional RD' },
  { value: 'FALLBACK', label: 'Reserva (fallback)' },
] as const

export function CreativeReviewTab() {
  const qc = useQueryClient()
  const table = useTableState({ defaultPageSize: 25, prefix: 'cr_', filterKeys: ['kind'] })
  const kind = table.state.filters.kind ?? 'INTERNAL'
  const params = { kind, status: 'RASCUNHO,EM_ANALISE', page: table.state.page, page_size: table.state.pageSize }
  const q = useQuery({ queryKey: qk.list('creatives', { review: params }), queryFn: () => creativesApi.list(params), placeholderData: keepPreviousData })
  const [approving, setApproving] = React.useState<Creative | null>(null)
  const [rejecting, setRejecting] = React.useState<Creative | null>(null)
  const [previewing, setPreviewing] = React.useState<Creative | null>(null)
  const refresh = () => void qc.invalidateQueries({ queryKey: qk.root('creatives') })

  const cols: DataTableColumn<Creative>[] = [
    {
      id: 'code',
      header: 'Anúncio',
      mobile: 'primary',
      cell: (c) => (
        <span className="flex flex-col">
          <span className="font-mono font-semibold">{c.code}</span>
          <span className="text-xs text-text-muted">{c.name}</span>
        </span>
      ),
    },
    { id: 'org', header: 'Organização', mobile: 'secondary', cell: (c) => <OrgName id={c.org_id} /> },
    { id: 'duration', header: 'Duração', numeric: true, cell: (c) => `${c.duration_seconds} s` },
    {
      id: 'status',
      header: 'Situação',
      mobile: 'badge',
      cell: (c) => (
        <span className="flex flex-wrap gap-1">
          <ToneBadge {...CREATIVE_STATUS[c.status]} />
          <ToneBadge {...(TECHNICAL_STATUS[c.technical_status] ?? { label: c.technical_status, tone: 'neutral' })} />
        </span>
      ),
    },
    { id: 'created', header: 'Criado em', cell: (c) => formatDateTime(c.created_at) },
  ]

  return (
    <div className="flex flex-col gap-4">
      <p className="m-0 text-sm text-text-muted">
        Anúncios de conteúdo interno, institucionais e de reserva também passam pela análise de conteúdo da RD. Anúncios de anunciantes são revisados na fila de
        campanhas.
      </p>
      <ToggleGroup type="single" value={kind} onValueChange={(v) => v && table.setFilter('kind', v)} aria-label="Tipo de anúncio">
        {KINDS.map((k) => (
          <ToggleGroupItem key={k.value} value={k.value}>
            {k.label}
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
      <DataTable
        caption="Anúncios aguardando revisão de conteúdo"
        columns={cols}
        data={q.data}
        getRowId={(c) => c.id}
        controller={table}
        isLoading={q.isLoading}
        isFetching={q.isFetching}
        error={q.error}
        onRetry={() => void q.refetch()}
        empty={{ title: 'Nada aguardando revisão', description: 'Novos anúncios deste tipo aparecem aqui.', icon: CheckCircle2 }}
        rowActions={(c) => (
          <>
            <Button variant="ghost" size="icon" aria-label={`Ver mídia de ${c.code}`} onClick={() => setPreviewing(c)}>
              <Eye aria-hidden="true" />
            </Button>
            <Button variant="secondary" onClick={() => setApproving(c)}>
              <CheckCircle2 aria-hidden="true" />
              Aprovar
            </Button>
            <Button variant="ghost" onClick={() => setRejecting(c)}>
              <XCircle aria-hidden="true" />
              Rejeitar
            </Button>
          </>
        )}
      />
      <ConfirmDialog
        open={!!approving}
        onOpenChange={(o) => !o && setApproving(null)}
        title={`Aprovar o anúncio ${approving?.code ?? ''}?`}
        description="O conteúdo passa a poder ser exibido nas telas onde estiver programado. A mídia precisa estar pronta e com duração válida."
        confirmLabel="Aprovar anúncio"
        onConfirm={async () => {
          await withDescribedError(() => creativesApi.review(approving!.id, 'APPROVE'))
          toast.success('Anúncio aprovado.')
          refresh()
        }}
      />
      <ReasonDialog
        open={!!rejecting}
        onOpenChange={(o) => !o && setRejecting(null)}
        destructive
        title={`Rejeitar o anúncio ${rejecting?.code ?? ''}?`}
        reasonLabel="Motivo da rejeição"
        minLength={10}
        confirmLabel="Rejeitar anúncio"
        onConfirm={async (reason) => {
          await withDescribedError(() => creativesApi.review(rejecting!.id, 'REJECT', reason))
          toast.success('Anúncio rejeitado.')
          refresh()
        }}
      />
      <CreativePreviewDialog creative={previewing} onClose={() => setPreviewing(null)} />
    </div>
  )
}

function CreativePreviewDialog({ creative, onClose }: { creative: Creative | null; onClose: () => void }) {
  const q = useQuery({
    queryKey: qk.detail('media', creative?.media_file_id ?? '-'),
    queryFn: () => mediaApi.get(creative!.media_file_id),
    enabled: !!creative,
  })
  if (!creative) return null
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>
            {creative.code} · {creative.name}
          </DialogTitle>
          <DialogDescription>{creative.ad_text ?? 'Sem texto informado.'}</DialogDescription>
        </DialogHeader>
        {q.isLoading ? <Skeleton className="h-48 w-full" /> : q.error ? <ErrorState error={q.error} size="compact" /> : q.data ? <MediaPreview media={q.data} className="max-h-80 w-full" /> : null}
        <DialogFooter>
          <Button variant="secondary" onClick={onClose}>
            Fechar
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
