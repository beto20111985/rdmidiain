/**
 * Biblioteca de mídia (PG-10; DS §10/§13; P001 §25–§28): enviar mídia com progresso real,
 * estados de processamento em tempo real (`media.processing`), metadados (tipo, dimensões,
 * orientação, duração, tamanho, codec), pré-visualização protegida (vídeo sem áudio),
 * renomear, reprocessar falha e excluir (bloqueado quando referenciada — com o motivo).
 * Backend: W-MEDIA `/media`. A equipe RD escolhe a organização dona do envio.
 */
import * as React from 'react'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Info, Pencil, RotateCcw, Trash2, Upload } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Field, FieldError, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { ConfirmDialog } from '@/components/patterns/confirm-dialog'
import { StaleData } from '@/components/patterns/states'
import { Can } from '@/components/auth/guards'
import { ToneBadge } from '@/components/campaigns/badges'
import { MediaPreview } from '@/components/campaigns/media-preview'
import { FilterSelect } from '@/components/campaigns/filter-select'
import { MediaUploader } from '@/components/campaigns/media-uploader'
import { EntityPicker, type PickerOption } from '@/components/campaigns/entity-picker'
import { OrgName } from '@/components/campaigns/org-name'
import { useTableState } from '@/hooks/useTableState'
import { useCan } from '@/hooks/usePermissions'
import { useRealtimeStatus } from '@/lib/realtime/context'
import { qk } from '@/lib/queryKeys'
import { isApiError } from '@/lib/errors'
import { EMPTY_VALUE, formatBytes, formatDateTime, formatNumber } from '@/lib/format'
import { mediaApi } from './lib/api'
import { MEDIA_STATUS } from './lib/labels'
import { msToClock } from './lib/format'
import { searchOrganizations } from './lib/pickers'
import type { MediaFile, MediaStatus } from './lib/types'
import { describeError } from '@/pages/campaigns/lib/errorText'

const REF_LABEL: Record<string, string> = {
  creatives: 'anúncio(s)',
  'inventory.manifests': 'programação de tela(s)',
}

function dims(m: MediaFile): string {
  const w = m.output?.width ?? m.source.width
  const h = m.output?.height ?? m.source.height
  return w && h ? `${w} × ${h}` : EMPTY_VALUE
}

export default function MediaLibraryPage() {
  const can = useCan()
  const qc = useQueryClient()
  const { live } = useRealtimeStatus()
  const table = useTableState({ defaultPageSize: 25, defaultSort: '-created_at', filterKeys: ['status', 'kind', 'orientation'] })
  const [uploadOpen, setUploadOpen] = React.useState(false)
  const [org, setOrg] = React.useState<PickerOption | null>(null)
  const [selected, setSelected] = React.useState<MediaFile | null>(null)

  const q = useQuery({
    queryKey: qk.list('media', table.apiParams),
    queryFn: () => mediaApi.list(table.apiParams),
    placeholderData: keepPreviousData,
  })

  const cols: DataTableColumn<MediaFile>[] = [
    {
      id: 'thumb',
      header: 'Prévia',
      mobile: 'hidden',
      cell: (m) => <MediaPreview media={m} thumbnail className="h-12 w-20" />,
    },
    {
      id: 'name',
      header: 'Nome',
      sortField: 'name',
      mobile: 'primary',
      cell: (m) => (
        <button
          type="button"
          className="text-left font-semibold text-link underline-offset-4 [overflow-wrap:anywhere] hover:underline"
          onClick={() => setSelected(m)}
        >
          {m.name}
        </button>
      ),
    },
    { id: 'kind', header: 'Tipo', mobile: 'secondary', cell: (m) => (m.kind === 'VIDEO' ? 'Vídeo' : 'Imagem') },
    {
      id: 'status',
      header: 'Status',
      mobile: 'badge',
      cell: (m) => {
        const s = MEDIA_STATUS[m.status]
        const label = m.status === 'PROCESSING' && m.progress_pct ? `${s.label} ${m.progress_pct}%` : s.label
        return <ToneBadge label={label} tone={s.tone} />
      },
    },
    { id: 'dims', header: 'Dimensões', cell: dims },
    {
      id: 'orientation',
      header: 'Orientação',
      cell: (m) => (m.output?.orientation === 'VERTICAL' ? 'Vertical' : m.output?.orientation === 'HORIZONTAL' ? 'Horizontal' : EMPTY_VALUE),
    },
    { id: 'duration', header: 'Duração', numeric: true, cell: (m) => (m.kind === 'VIDEO' ? msToClock(m.output?.duration_ms ?? m.source.duration_ms) : EMPTY_VALUE) },
    { id: 'size', header: 'Tamanho', numeric: true, sortField: 'size', cell: (m) => formatBytes(m.output?.size_bytes ?? m.source.size_bytes) },
    { id: 'org', header: 'Organização', mobile: 'hidden', cell: (m) => <OrgName id={m.org_id} /> },
    { id: 'created', header: 'Enviada em', sortField: 'created_at', cell: (m) => formatDateTime(m.created_at) },
  ]

  const filtersActive = !!table.state.q || Object.keys(table.state.filters).length > 0
  const total = q.data?.total

  return (
    <>
      <PageHeader
        title="Biblioteca de mídia"
        description={total !== undefined ? `${formatNumber(total)} arquivo(s)` : undefined}
        actions={
          <Can permission="media.upload">
            <Button onClick={() => setUploadOpen((o) => !o)} aria-expanded={uploadOpen}>
              <Upload aria-hidden="true" />
              Enviar mídia
            </Button>
          </Can>
        }
      />
      <div className="flex flex-col gap-6">
        {!live && q.data ? <StaleData updatedAt={q.dataUpdatedAt} reason="tempo real indisponível; o status de processamento pode estar desatualizado." onRefresh={() => void q.refetch()} refreshing={q.isFetching} /> : null}
        {uploadOpen && can('media.upload') ? (
          <Card>
            <CardHeader>
              <CardTitle>Enviar mídia</CardTitle>
            </CardHeader>
            <EntityPicker
              label="Organização dona da mídia"
              domain="organizations"
              value={org}
              onChange={setOrg}
              required
              description="A mídia pertence a uma organização (anunciante ou parceira) e só aparece para ela e para a RD."
              search={(t) => searchOrganizations(t)}
            />
            <MediaUploader orgId={org?.id ?? null} requireOrg onReady={() => void qc.invalidateQueries({ queryKey: qk.root('media') })} />
          </Card>
        ) : null}
        <Card>
          <DataTable
            caption="Biblioteca de mídia"
            columns={cols}
            data={q.data}
            getRowId={(m) => m.id}
            controller={table}
            isLoading={q.isLoading}
            isFetching={q.isFetching}
            error={q.error}
            onRetry={() => void q.refetch()}
            filtersActive={filtersActive}
            empty={{
              title: 'Envie imagens ou vídeos para montar suas campanhas.',
              action: can('media.upload') ? <Button onClick={() => setUploadOpen(true)}>Enviar mídia</Button> : undefined,
            }}
            rowActions={(m) => (
              <Button variant="ghost" size="icon" aria-label={`Detalhes de ${m.name}`} onClick={() => setSelected(m)}>
                <Info aria-hidden="true" />
              </Button>
            )}
            toolbar={
              <>
                <Field className="max-w-xs">
                  <FieldLabel>Buscar</FieldLabel>
                  <Input type="search" value={table.state.q} onChange={(e) => table.setQuery(e.target.value)} placeholder="Nome do arquivo" />
                </Field>
                <FilterSelect
                  label="Status"
                  value={table.state.filters.status}
                  onChange={(v) => table.setFilter('status', v)}
                  options={(Object.keys(MEDIA_STATUS) as MediaStatus[]).map((s) => ({ value: s, label: MEDIA_STATUS[s].label }))}
                />
                <FilterSelect
                  label="Tipo"
                  value={table.state.filters.kind}
                  onChange={(v) => table.setFilter('kind', v)}
                  options={[
                    { value: 'IMAGE', label: 'Imagem' },
                    { value: 'VIDEO', label: 'Vídeo' },
                  ]}
                />
                <FilterSelect
                  label="Orientação"
                  value={table.state.filters.orientation}
                  onChange={(v) => table.setFilter('orientation', v)}
                  options={[
                    { value: 'HORIZONTAL', label: 'Horizontal' },
                    { value: 'VERTICAL', label: 'Vertical' },
                  ]}
                />
              </>
            }
          />
        </Card>
      </div>
      <MediaDetailsDialog media={selected} onClose={() => setSelected(null)} />
    </>
  )
}

function MediaDetailsDialog({ media, onClose }: { media: MediaFile | null; onClose: () => void }) {
  const qc = useQueryClient()
  const can = useCan()
  const [name, setName] = React.useState('')
  const [nameError, setNameError] = React.useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = React.useState(false)

  const detail = useQuery({
    queryKey: qk.detail('media', media?.id ?? '-'),
    queryFn: () => mediaApi.get(media!.id),
    enabled: !!media,
    initialData: media ?? undefined,
  })
  const m = detail.data ?? media
  const usage = useQuery({
    queryKey: qk.sub('media', 'detail', media?.id ?? '-', 'usage'),
    queryFn: () => mediaApi.usage(media!.id),
    enabled: !!media && can('media.delete'),
  })

  React.useEffect(() => {
    setName(media?.name ?? '')
    setNameError(null)
  }, [media])

  const rename = useMutation({
    mutationFn: () => mediaApi.rename(m!.id, name.trim()),
    onSuccess: () => {
      toast.success('Nome atualizado.')
      void qc.invalidateQueries({ queryKey: qk.root('media') })
    },
    onError: (e) => setNameError(describeError(e)),
  })
  const retry = useMutation({
    mutationFn: () => mediaApi.retry(m!.id),
    onSuccess: () => {
      toast.success('Reprocessamento solicitado.')
      void qc.invalidateQueries({ queryKey: qk.root('media') })
    },
    onError: (e) => toast.error(describeError(e)),
  })

  if (!m) return null
  const inUse = usage.data?.in_use ?? false
  const refs = Object.entries(usage.data?.references ?? {}).filter(([, n]) => n > 0)
  const refText = refs.map(([k, n]) => `${n} ${REF_LABEL[k] ?? k}`).join(', ')
  const out = m.output
  const meta: Array<[string, React.ReactNode]> = [
    ['Tipo', m.kind === 'VIDEO' ? `Vídeo (${out?.mime_type ?? m.source.mime})` : `Imagem (${out?.mime_type ?? m.source.mime})`],
    ['Dimensões', dims(m)],
    ['Orientação', out?.orientation === 'VERTICAL' ? 'Vertical' : out?.orientation === 'HORIZONTAL' ? 'Horizontal' : EMPTY_VALUE],
    ['Duração', m.kind === 'VIDEO' ? msToClock(out?.duration_ms ?? m.source.duration_ms) : 'Definida pelo anúncio (imagem)'],
    ['Tamanho', `${formatBytes(out?.size_bytes ?? m.source.size_bytes)}${out ? ` (original ${formatBytes(m.source.size_bytes)})` : ''}`],
    ['Codec', out?.codec ?? m.source.codec ?? EMPTY_VALUE],
    ['Quadros/s', out?.fps_milli ? formatNumber(out.fps_milli / 1000, { maximumFractionDigits: 2 }) : EMPTY_VALUE],
    ['Áudio', m.kind === 'VIDEO' ? (m.source.has_audio ? 'Removido no processamento' : 'Sem áudio') : EMPTY_VALUE],
    ['Arquivo original', m.original_filename],
    ['Checksum (SHA-256)', out?.sha256 ? `${out.sha256.slice(0, 16)}…` : EMPTY_VALUE],
    ['Enviada em', formatDateTime(m.created_at)],
  ]

  return (
    <>
      <Dialog open={!!media} onOpenChange={(o) => !o && onClose()}>
        <DialogContent size="md">
          <DialogHeader>
            <DialogTitle>{m.name}</DialogTitle>
            <DialogDescription>
              <span className="inline-flex items-center gap-2">
                <ToneBadge label={MEDIA_STATUS[m.status].label} tone={MEDIA_STATUS[m.status].tone} />
                {m.status === 'PROCESSING' ? `${m.progress_pct}%` : null}
              </span>
            </DialogDescription>
          </DialogHeader>
          <div className="flex max-h-[60vh] flex-col gap-4 overflow-y-auto">
            {m.status === 'REJECTED' || m.status === 'FAILED' ? (
              <Alert variant="danger">
                <AlertTitle>{m.status === 'REJECTED' ? 'Arquivo recusado' : 'Falha no processamento'}</AlertTitle>
                <AlertDescription>
                  {m.status_message ?? 'Sem detalhes.'} {m.status_code ? `(${m.status_code})` : ''}
                </AlertDescription>
              </Alert>
            ) : null}
            {m.extension_mismatch ? (
              <p className="m-0 text-xs text-text-muted">A extensão do nome não corresponde ao conteúdo real; o tipo real prevaleceu.</p>
            ) : null}
            <MediaPreview media={m} className="max-h-72 w-full" />
            <dl className="m-0 grid grid-cols-1 gap-x-4 gap-y-2 text-sm sm:grid-cols-2">
              {meta.map(([k, v]) => (
                <div key={k} className="flex min-w-0 flex-col">
                  <dt className="text-xs text-text-muted">{k}</dt>
                  <dd className="m-0 break-words">{v}</dd>
                </div>
              ))}
            </dl>
            {can('media.upload') ? (
              <form
                className="flex flex-wrap items-end gap-3"
                onSubmit={(e) => {
                  e.preventDefault()
                  if (!name.trim()) {
                    setNameError('Informe um nome.')
                    return
                  }
                  setNameError(null)
                  rename.mutate()
                }}
              >
                <Field invalid={!!nameError} className="min-w-0 flex-1">
                  <FieldLabel>Renomear</FieldLabel>
                  <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={255} />
                  <FieldError>{nameError}</FieldError>
                </Field>
                <Button type="submit" variant="secondary" loading={rename.isPending} disabled={name.trim() === m.name}>
                  <Pencil aria-hidden="true" />
                  Salvar nome
                </Button>
              </form>
            ) : null}
            {can('media.delete') && inUse ? (
              <Alert variant="warning">
                <AlertTitle>Exclusão bloqueada</AlertTitle>
                <AlertDescription>Esta mídia está em uso ({refText || 'referências ativas'}) e não pode ser excluída.</AlertDescription>
              </Alert>
            ) : null}
          </div>
          <DialogFooter>
            {m.status === 'FAILED' && can('media.upload') ? (
              <Button variant="secondary" onClick={() => retry.mutate()} loading={retry.isPending}>
                <RotateCcw aria-hidden="true" />
                Reprocessar
              </Button>
            ) : null}
            {can('media.delete') ? (
              <Button
                variant="destructive"
                onClick={() => setConfirmDelete(true)}
                disabled={inUse || usage.isLoading}
                title={inUse ? 'Mídia em uso por anúncios ou programação' : undefined}
              >
                <Trash2 aria-hidden="true" />
                Excluir
              </Button>
            ) : null}
            <Button variant="secondary" onClick={onClose}>
              Fechar
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        destructive
        title={`Excluir a mídia ${m.name}?`}
        description="A mídia some da biblioteca. O arquivo é apagado definitivamente após o período de carência, se continuar sem uso."
        consequences={['Não poderá ser usada em novos anúncios.', 'Exclusão é recusada se a mídia passar a ser usada antes da confirmação.']}
        requireText={m.name}
        confirmLabel="Excluir mídia"
        onConfirm={async () => {
          try {
            await mediaApi.remove(m.id)
          } catch (e) {
            if (isApiError(e) && e.code === 'MEDIA_IN_USE') {
              const r = (e.details.references ?? {}) as Record<string, number>
              const t = Object.entries(r).map(([k, n]) => `${n} ${REF_LABEL[k] ?? k}`).join(', ')
              throw new Error(`Mídia em uso${t ? ` (${t})` : ''}: não pode ser excluída.`)
            }
            throw e
          }
          toast.success('Mídia excluída.')
          onClose()
          void qc.invalidateQueries({ queryKey: qk.root('media') })
        }}
      />
    </>
  )
}
