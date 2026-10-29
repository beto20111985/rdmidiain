/**
 * Peças da RD (conteúdo interno, contingência e institucional), fora de campanha:
 *  - cadastro a partir de mídia PRONTA da biblioteca (POST /creatives com kind ≠ EXTERNAL —
 *    só a equipe RD com acesso global; a peça nasce "aguardando revisão");
 *  - revisão de conteúdo pela rota POST /creatives/{id}/review (campaigns.approve GLOBAL):
 *    aprovar exige mídia pronta e duração válida; rejeitar exige motivo (≥ 10 caracteres).
 * Rejeitada não volta a ser revisada: a correção é uma nova versão (novo código RD).
 * Só peças APROVADAS entram no conteúdo interno / contingência (o backend valida).
 */
import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Check, Clapperboard, Plus, X } from 'lucide-react'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { ConfirmDialog } from '@/components/patterns/confirm-dialog'
import { EmptyState, ErrorState, NoPermission } from '@/components/patterns/states'
import { Can } from '@/components/auth/guards'
import { FilterSelect, OptionSelect } from '@/components/ops/filter-select'
import { MediaPicker } from '@/components/ops/pickers'
import { ReasonDialog } from '@/components/ops/reason-dialog'
import { useCan } from '@/hooks/usePermissions'
import { errorMessage, isApiError } from '@/lib/errors'
import { formatDateTime } from '@/lib/format'
import { qk } from '@/lib/queryKeys'
import { creativesApi } from '@/pages/ops/api/client'
import {
  CREATIVE_KIND_LABEL,
  CREATIVE_STATUS_LABEL,
  CREATIVE_STATUS_TONE,
  TECHNICAL_STATUS_LABEL,
  labelOf,
} from '@/pages/ops/api/labels'
import type { CreativeLite, MediaLite, RdPieceKind } from '@/pages/ops/api/types'
import { ALLOWED_DURATIONS, suggestDuration } from './rdPieceRules'

const PAGE_SIZE = 25
const REVIEWABLE = new Set(['RASCUNHO', 'EM_ANALISE'])
const PENDING = 'RASCUNHO,EM_ANALISE'

const KIND_OPTIONS: Array<{ value: RdPieceKind; label: string }> = [
  { value: 'INTERNAL', label: CREATIVE_KIND_LABEL.INTERNAL },
  { value: 'FALLBACK', label: CREATIVE_KIND_LABEL.FALLBACK },
  { value: 'INSTITUTIONAL', label: CREATIVE_KIND_LABEL.INSTITUTIONAL },
]
const KIND_HINT: Record<RdPieceKind, string> = {
  INTERNAL: 'Ocupa os blocos internos do estabelecimento (lista comum ou grade própria).',
  FALLBACK: 'Usada quando um bloco fica sem conteúdo válido.',
  INSTITUTIONAL: 'Peça institucional da RD (também pode servir de contingência).',
}
const STATUS_OPTIONS = [
  { value: PENDING, label: 'Aguardando revisão' },
  { value: 'APROVADO', label: 'Aprovadas' },
  { value: 'REJEITADO', label: 'Rejeitadas' },
]

export function RdPiecesPanel() {
  const can = useCan()
  const qc = useQueryClient()
  const [kind, setKind] = React.useState<RdPieceKind>('INTERNAL')
  const [status, setStatus] = React.useState<string | null>(PENDING)
  const [page, setPage] = React.useState(1)
  const [creating, setCreating] = React.useState(false)
  const [approving, setApproving] = React.useState<CreativeLite | null>(null)
  const [rejecting, setRejecting] = React.useState<CreativeLite | null>(null)

  const list = useQuery({
    queryKey: qk.sub('creatives', 'rd-pieces', kind, status ?? 'all', page),
    queryFn: () => creativesApi.list({ kind, status: status ?? undefined, page, page_size: PAGE_SIZE }),
  })
  const invalidate = () => void qc.invalidateQueries({ queryKey: ['creatives'] })

  if (isApiError(list.error) && list.error.isForbidden)
    return <NoPermission permission="creatives.view" title="Peças da RD são geridas pela equipe RD" />

  const items = list.data?.items ?? []
  const total = list.data?.total ?? 0
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const canReview = can('campaigns.approve')

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader>
          <div className="flex flex-col gap-1">
            <CardTitle>Peças da RD</CardTitle>
            <CardDescription>
              Conteúdo interno, contingência e institucional. Toda peça nova passa pela revisão de conteúdo antes de entrar nas listas.
            </CardDescription>
          </div>
          <Can permission="creatives.manage">
            <Button variant="secondary" onClick={() => setCreating((o) => !o)} aria-expanded={creating}>
              <Plus aria-hidden="true" />
              Nova peça
            </Button>
          </Can>
        </CardHeader>
        {creating ? (
          <NewPieceForm
            defaultKind={kind}
            onCancel={() => setCreating(false)}
            onCreated={(c) => {
              invalidate()
              setCreating(false)
              setKind(c.kind as RdPieceKind)
              setStatus(PENDING)
              setPage(1)
              toast.success(`Peça ${c.code} cadastrada. Ela fica aguardando revisão de conteúdo.`)
            }}
          />
        ) : null}
        <div className="flex flex-col gap-3 sm:flex-row sm:flex-wrap">
          <Field className="w-full sm:w-56">
            <FieldLabel>Tipo</FieldLabel>
            <OptionSelect
              value={kind}
              onChange={(v) => {
                if (!v) return
                setKind(v as RdPieceKind)
                setPage(1)
              }}
              options={KIND_OPTIONS}
            />
          </Field>
          <FilterSelect
            label="Situação"
            value={status}
            onChange={(v) => {
              setStatus(v)
              setPage(1)
            }}
            options={STATUS_OPTIONS}
            allLabel="Todas"
          />
        </div>
        {list.isLoading ? (
          <Skeleton className="h-32 w-full" />
        ) : list.error ? (
          <ErrorState error={list.error} onRetry={() => void list.refetch()} size="compact" />
        ) : !items.length ? (
          <EmptyState
            icon={Clapperboard}
            size="compact"
            headingLevel="h3"
            title={status === PENDING ? 'Nenhuma peça aguardando revisão.' : 'Nenhuma peça encontrada.'}
            description="Peças da RD são cadastradas a partir de mídias prontas da biblioteca."
          />
        ) : (
          <>
            <ul className="m-0 flex list-none flex-col divide-y divide-border p-0" aria-label="Peças da RD">
              {items.map((c) => (
                <li key={c.id} className="flex flex-col gap-2 py-3 sm:flex-row sm:items-center sm:justify-between">
                  <div className="flex min-w-0 flex-col gap-1">
                    <span className="break-words font-semibold text-text">
                      <span className="font-mono">{c.code}</span> — {c.name}
                    </span>
                    <span className="text-xs text-text-muted">
                      {labelOf(CREATIVE_KIND_LABEL, c.kind)} · {c.duration_seconds ? `${c.duration_seconds} s` : '—'}
                      {c.created_at ? ` · cadastrada em ${formatDateTime(c.created_at)}` : ''}
                    </span>
                    {c.technical_status && c.technical_status !== 'VALIDO' ? (
                      <span className="text-xs text-text-muted">
                        {labelOf(TECHNICAL_STATUS_LABEL, c.technical_status)}
                        {c.technical_error ? ` (${c.technical_error})` : ''}
                      </span>
                    ) : null}
                    {c.status === 'REJEITADO' && c.rejection_reason ? (
                      <span className="break-words text-xs text-text-muted">Motivo da rejeição: {c.rejection_reason}</span>
                    ) : null}
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge tone={CREATIVE_STATUS_TONE[c.status] ?? 'neutral'}>{labelOf(CREATIVE_STATUS_LABEL, c.status)}</Badge>
                    {canReview && REVIEWABLE.has(c.status) && c.kind !== 'EXTERNAL' ? (
                      <>
                        <Button onClick={() => setApproving(c)} aria-label={`Aprovar a peça ${c.code}`}>
                          <Check aria-hidden="true" />
                          Aprovar
                        </Button>
                        <Button variant="secondary" onClick={() => setRejecting(c)} aria-label={`Rejeitar a peça ${c.code}`}>
                          <X aria-hidden="true" />
                          Rejeitar
                        </Button>
                      </>
                    ) : null}
                  </div>
                </li>
              ))}
            </ul>
            {pages > 1 ? (
              <nav className="flex flex-wrap items-center justify-between gap-2" aria-label="Paginação das peças">
                <span className="text-sm text-text-muted">
                  Página {page} de {pages} · {total} peças
                </span>
                <div className="flex gap-2">
                  <Button variant="secondary" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
                    Anterior
                  </Button>
                  <Button variant="secondary" disabled={page >= pages} onClick={() => setPage((p) => p + 1)}>
                    Próxima
                  </Button>
                </div>
              </nav>
            ) : null}
          </>
        )}
        {!canReview ? (
          <p className="m-0 text-xs text-text-muted">A revisão de conteúdo exige a permissão campaigns.approve com acesso global.</p>
        ) : null}
      </Card>

      <ConfirmDialog
        open={!!approving}
        onOpenChange={(o) => !o && setApproving(null)}
        title={`Aprovar a peça ${approving?.code ?? ''}?`}
        description={approving ? `${approving.name} · ${labelOf(CREATIVE_KIND_LABEL, approving.kind)}` : undefined}
        consequences={[
          'A peça passa a poder entrar no conteúdo interno ou na contingência das telas.',
          'A aprovação fica registrada no histórico de auditoria com o seu usuário.',
        ]}
        confirmLabel="Aprovar peça"
        onConfirm={async () => {
          const c = await creativesApi.review(approving!.id, { decision: 'APPROVE' })
          invalidate()
          toast.success(`Peça ${c.code} aprovada.`)
        }}
      />
      <ReasonDialog
        open={!!rejecting}
        onOpenChange={(o) => !o && setRejecting(null)}
        title={`Rejeitar a peça ${rejecting?.code ?? ''}?`}
        description="Peça rejeitada não volta para revisão: a correção é uma nova versão, com novo código RD."
        confirmLabel="Rejeitar peça"
        destructive
        minReason={10}
        reasonLabel="Motivo da rejeição"
        onConfirm={async (reason) => {
          const c = await creativesApi.review(rejecting!.id, { decision: 'REJECT', reason })
          invalidate()
          toast.success(`Peça ${c.code} rejeitada.`)
        }}
      />
    </div>
  )
}

function NewPieceForm({
  defaultKind,
  onCancel,
  onCreated,
}: {
  defaultKind: RdPieceKind
  onCancel: () => void
  onCreated: (c: CreativeLite) => void
}) {
  const [name, setName] = React.useState('')
  const [kind, setKind] = React.useState<RdPieceKind>(defaultKind)
  const [media, setMedia] = React.useState<MediaLite | undefined>(undefined)
  const [mediaId, setMediaId] = React.useState<string | null>(null)
  const [duration, setDuration] = React.useState<string | null>(null)
  const [submitted, setSubmitted] = React.useState(false)
  const nameRef = React.useRef<HTMLInputElement>(null)

  const create = useMutation({
    mutationFn: () =>
      creativesApi.create({
        org_id: media!.org_id,
        name: name.trim(),
        media_file_id: media!.id,
        duration_seconds: Number(duration),
        kind,
      }),
    onSuccess: onCreated,
  })
  const nameError = submitted && name.trim().length < 2 ? 'Informe o nome da peça (mín. 2 caracteres).' : null
  const mediaError = submitted && !media ? 'Escolha uma mídia pronta da biblioteca.' : null
  const durationError = submitted && !duration ? 'Escolha a duração.' : null
  const mediaMs = media ? (media.output?.duration_ms ?? media.source.duration_ms) : null

  return (
    <form
      className="flex flex-col gap-4 rounded-lg border border-border p-4"
      noValidate
      onSubmit={(e) => {
        e.preventDefault()
        setSubmitted(true)
        if (name.trim().length < 2) nameRef.current?.focus()
        else if (media && duration) create.mutate()
      }}
    >
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Field required invalid={!!nameError}>
          <FieldLabel>Nome da peça</FieldLabel>
          <Input ref={nameRef} value={name} onChange={(e) => setName(e.target.value)} maxLength={255} autoComplete="off" />
          <FieldError>{nameError}</FieldError>
        </Field>
        <Field required>
          <FieldLabel>Tipo</FieldLabel>
          <OptionSelect value={kind} onChange={(v) => v && setKind(v as RdPieceKind)} options={KIND_OPTIONS} />
          <FieldDescription>{KIND_HINT[kind]}</FieldDescription>
        </Field>
      </div>
      <MediaPicker
        value={mediaId}
        selectedLabel={media?.name}
        onChange={(id, item) => {
          setMediaId(id)
          if (item) {
            setMedia(item)
            const s = suggestDuration(item)
            if (s) setDuration(String(s))
          }
        }}
        required
        error={mediaError}
        description="Somente mídias já processadas (prontas). A peça usa a organização da mídia escolhida."
      />
      <Field required invalid={!!durationError} className="sm:max-w-xs">
        <FieldLabel>Duração no bloco</FieldLabel>
        <OptionSelect
          value={duration}
          onChange={setDuration}
          options={ALLOWED_DURATIONS.map((d) => ({ value: String(d), label: `${d} segundos` }))}
        />
        <FieldDescription>
          {media?.kind === 'VIDEO' && mediaMs
            ? `O vídeo tem ${(mediaMs / 1000).toFixed(1).replace('.', ',')} s; a duração precisa ser compatível (a mídia nunca é cortada).`
            : 'Imagens exibem pelo tempo escolhido.'}
        </FieldDescription>
        <FieldError>{durationError}</FieldError>
      </Field>
      {create.error ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">{errorMessage(create.error)}</AlertDescription>
        </Alert>
      ) : null}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" loading={create.isPending}>
          Cadastrar peça
        </Button>
        <Button type="button" variant="secondary" onClick={onCancel} disabled={create.isPending}>
          Cancelar
        </Button>
      </div>
    </form>
  )
}
