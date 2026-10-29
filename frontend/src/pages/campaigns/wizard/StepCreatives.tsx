/**
 * Etapa 2 — Mídia e anúncios: duração do anúncio (30, 60 ou 120 s — 15/45/90 são recusados
 * aqui e pelo servidor), anúncios vinculados com código RD, criação de anúncio a partir da
 * biblioteca (ou envio na hora, com progresso real) e verificação de que o vídeo cabe no slot.
 */
import * as React from 'react'
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link2, Plus, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Button } from '@/components/ui/button'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ToneBadge } from '@/components/campaigns/badges'
import { MediaUploader } from '@/components/campaigns/media-uploader'
import { MediaPreview } from '@/components/campaigns/media-preview'
import { qk } from '@/lib/queryKeys'
import { campaignsApi, creativesApi, mediaApi } from '../lib/api'
import { CREATIVE_STATUS, TECHNICAL_STATUS } from '../lib/labels'
import { ALLOWED_DURATIONS, durationSchema, serverFieldErrors, videoFitsSlot } from '../lib/rules'
import { msToClock } from '../lib/format'
import type { MediaFile } from '../lib/types'
import { ServerError, StepActions, type StepProps } from './shared'
import { describeError } from '@/pages/campaigns/lib/errorText'

export function StepCreatives({ campaign, onSaved, readOnly }: StepProps) {
  const qc = useQueryClient()
  const [duration, setDuration] = React.useState<number>(campaign?.duration_s ?? 30)
  const [durationError, setDurationError] = React.useState<string | null>(null)
  const [serverError, setServerError] = React.useState<unknown>(null)
  const [saving, setSaving] = React.useState<'draft' | 'next' | null>(null)
  const [creating, setCreating] = React.useState(false)

  if (!campaign) return <Alert variant="info"><AlertDescription>Salve a identificação primeiro.</AlertDescription></Alert>
  const links = campaign.creatives.filter((c) => !c.pending_removal)
  const durationDirty = duration !== campaign.duration_s

  const refresh = async () => {
    const fresh = await campaignsApi.get(campaign.id)
    qc.setQueryData(qk.detail('campaigns', campaign.id), fresh)
    return fresh
  }

  const saveDuration = async (advance: boolean) => {
    if (readOnly) return
    const parsed = durationSchema.safeParse(duration)
    if (!parsed.success) {
      setDurationError(parsed.error.issues[0]?.message ?? 'Duração inválida.')
      return
    }
    setDurationError(null)
    setServerError(null)
    setSaving(advance ? 'next' : 'draft')
    try {
      let saved = campaign
      if (durationDirty) {
        saved = await campaignsApi.update(campaign.id, { duration_s: parsed.data, expected_version: campaign.version })
        qc.setQueryData(qk.detail('campaigns', campaign.id), saved)
        toast.success('Duração salva.')
      }
      onSaved(saved, advance)
    } catch (e) {
      const fe = serverFieldErrors(e)
      if (fe.duration_s) setDurationError(fe.duration_s)
      else setServerError(e)
    } finally {
      setSaving(null)
    }
  }

  const unlink = async (creativeId: string) => {
    try {
      await campaignsApi.unlinkCreative(campaign.id, creativeId)
      await refresh()
      toast.success('Anúncio removido da campanha.')
    } catch (e) {
      toast.error(describeError(e))
    }
  }

  const mismatched = links.filter((l) => l.technical_status === 'INVALIDO')

  return (
    <div className="flex flex-col gap-6">
      <Field invalid={!!durationError}>
        <FieldLabel id="duration-label" required>
          Duração do anúncio
        </FieldLabel>
        <ToggleGroup
          type="single"
          value={String(duration)}
          onValueChange={(v) => v && setDuration(Number(v))}
          aria-labelledby="duration-label"
          disabled={readOnly}
          data-testid="duration-toggle"
        >
          {ALLOWED_DURATIONS.map((d) => (
            <ToggleGroupItem key={d} value={String(d)}>
              {d} segundos
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
        <FieldDescription>Unidade de venda de 30 s. Vídeos precisam ter a mesma duração do slot (nunca cortamos a mídia).</FieldDescription>
        <FieldError>{durationError}</FieldError>
      </Field>
      {durationDirty && links.length ? (
        <Alert variant="warning">
          <AlertDescription>Os anúncios já vinculados têm {campaign.duration_s} s. Ao mudar a duração, substitua-os por anúncios da nova duração.</AlertDescription>
        </Alert>
      ) : null}

      <section className="flex flex-col gap-3" aria-labelledby="linked-creatives">
        <h3 id="linked-creatives" className="text-title-card">
          Anúncios da campanha
        </h3>
        {links.length === 0 ? (
          <p className="m-0 text-sm text-text-muted">
            Nenhum anúncio vinculado. Você pode pagar antes e enviar a mídia depois (a campanha fica “Aguardando mídia”).
          </p>
        ) : (
          <Table>
            <caption className="sr-only">Anúncios vinculados</caption>
            <TableHeader>
              <TableRow className="h-11 hover:bg-transparent">
                <TableHead scope="col">Código</TableHead>
                <TableHead scope="col">Nome</TableHead>
                <TableHead scope="col">Situação</TableHead>
                <TableHead scope="col" className="text-right">
                  Ações
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {links.map((l) => (
                <TableRow key={l.creative_id}>
                  <TableCell className="font-mono font-semibold">{l.code ?? '—'}</TableCell>
                  <TableCell>{l.name ?? '—'}</TableCell>
                  <TableCell>
                    <span className="flex flex-wrap gap-1">
                      {l.status ? <ToneBadge {...CREATIVE_STATUS[l.status]} /> : null}
                      {l.technical_status ? <ToneBadge {...(TECHNICAL_STATUS[l.technical_status] ?? { label: l.technical_status, tone: 'neutral' })} /> : null}
                    </span>
                  </TableCell>
                  <TableCell className="text-right">
                    {!readOnly ? (
                      <Button variant="ghost" size="icon" aria-label={`Remover ${l.code ?? 'anúncio'} da campanha`} onClick={() => void unlink(l.creative_id)}>
                        <Trash2 aria-hidden="true" />
                      </Button>
                    ) : null}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
        {mismatched.length ? (
          <Alert variant="danger">
            <AlertDescription>Há anúncio(s) com mídia inválida para esta duração. Substitua-os antes do pagamento.</AlertDescription>
          </Alert>
        ) : null}
      </section>

      {!readOnly ? (
        creating ? (
          <NewCreative
            orgId={campaign.org_id}
            campaignId={campaign.id}
            duration={campaign.duration_s}
            onDone={async () => {
              setCreating(false)
              await refresh()
            }}
            onCancel={() => setCreating(false)}
          />
        ) : (
          <div className="flex flex-wrap gap-3">
            <Button variant="secondary" onClick={() => setCreating(true)} disabled={durationDirty}>
              <Plus aria-hidden="true" />
              Adicionar anúncio
            </Button>
            {durationDirty ? <span className="self-center text-xs text-text-muted">Salve a nova duração antes de adicionar anúncios.</span> : null}
          </div>
        )
      ) : null}

      <ServerError error={serverError} />
      {!readOnly ? <StepActions saving={saving} onSave={(a) => void saveDuration(a)} /> : null}
    </div>
  )
}

export function NewCreative({
  orgId,
  campaignId,
  duration,
  onDone,
  onCancel,
}: {
  orgId: string
  campaignId: string
  duration: number
  onDone: () => Promise<void> | void
  onCancel: () => void
}) {
  const qc = useQueryClient()
  const [mode, setMode] = React.useState<'existing' | 'media'>('existing')
  const [existingId, setExistingId] = React.useState('')
  const [mediaId, setMediaId] = React.useState('')
  const [uploaded, setUploaded] = React.useState<MediaFile | null>(null)
  const [name, setName] = React.useState('')
  const [adText, setAdText] = React.useState('')
  const [errors, setErrors] = React.useState<Record<string, string>>({})
  const [serverError, setServerError] = React.useState<unknown>(null)
  const [pending, setPending] = React.useState(false)

  const existing = useQuery({
    queryKey: qk.list('creatives', { org_id: orgId, status: 'RASCUNHO,APROVADO', page_size: 100 }),
    queryFn: () => creativesApi.list({ org_id: orgId, status: 'RASCUNHO,APROVADO', page_size: 100 }),
  })
  const media = useQuery({
    queryKey: qk.list('media', { org_id: orgId, status: 'READY', page_size: 100 }),
    queryFn: () => mediaApi.list({ org_id: orgId, status: 'READY', page_size: 100 }),
    placeholderData: keepPreviousData,
  })
  const usableCreatives = (existing.data?.items ?? []).filter((c) => c.duration_seconds === duration && c.technical_status !== 'INVALIDO')
  const mediaList = media.data?.items ?? []
  const chosenMedia = uploaded && uploaded.id === mediaId ? uploaded : mediaList.find((m) => m.id === mediaId)
  const fits = chosenMedia?.kind === 'VIDEO' ? videoFitsSlot(chosenMedia.output?.duration_ms, duration) : true

  const submit = async () => {
    setServerError(null)
    const e: Record<string, string> = {}
    if (mode === 'existing' && !existingId) e.existing = 'Escolha um anúncio.'
    if (mode === 'media') {
      if (!mediaId) e.media = 'Escolha ou envie a mídia.'
      if (name.trim().length < 2) e.name = 'Dê um nome ao anúncio (mínimo 2 caracteres).'
      if (fits === false)
        e.media = `O vídeo tem ${msToClock(chosenMedia?.output?.duration_ms)} e não cabe no anúncio de ${duration} s (nunca cortamos a mídia).`
    }
    setErrors(e)
    if (Object.keys(e).length) return
    setPending(true)
    try {
      let creativeId = existingId
      if (mode === 'media') {
        const c = await creativesApi.create({ org_id: orgId, name: name.trim(), media_file_id: mediaId, duration_seconds: duration, ad_text: adText.trim() || null })
        creativeId = c.id
        toast.success(`Anúncio ${c.code} criado.`)
      }
      await campaignsApi.linkCreative(campaignId, creativeId)
      void qc.invalidateQueries({ queryKey: qk.root('creatives') })
      await onDone()
    } catch (err) {
      const fe = serverFieldErrors(err)
      if (fe.duration_s) setErrors({ media: fe.duration_s })
      else setServerError(err)
    } finally {
      setPending(false)
    }
  }

  return (
    <section className="flex flex-col gap-4 rounded-lg border border-border p-4" aria-labelledby="new-creative-title">
      <h3 id="new-creative-title" className="text-title-card">
        Adicionar anúncio de {duration} s
      </h3>
      <ToggleGroup type="single" value={mode} onValueChange={(v) => v && setMode(v as 'existing' | 'media')} aria-label="Origem do anúncio">
        <ToggleGroupItem value="existing">Anúncio existente</ToggleGroupItem>
        <ToggleGroupItem value="media">Novo a partir de mídia</ToggleGroupItem>
      </ToggleGroup>
      {mode === 'existing' ? (
        <Field invalid={!!errors.existing}>
          <FieldLabel>Anúncio</FieldLabel>
          <Select value={existingId} onValueChange={setExistingId}>
            <SelectTrigger>
              <SelectValue placeholder={existing.isLoading ? 'Carregando…' : usableCreatives.length ? 'Selecione' : 'Nenhum anúncio desta duração'} />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                {usableCreatives.map((c) => (
                  <SelectItem key={c.id} value={c.id}>
                    {c.code} · {c.name}
                  </SelectItem>
                ))}
              </SelectGroup>
            </SelectContent>
          </Select>
          <FieldError>{errors.existing}</FieldError>
        </Field>
      ) : (
        <div className="flex flex-col gap-4">
          <Field invalid={!!errors.media}>
            <FieldLabel>Mídia da biblioteca (prontas)</FieldLabel>
            <Select value={mediaId} onValueChange={setMediaId}>
              <SelectTrigger>
                <SelectValue placeholder={media.isLoading ? 'Carregando…' : mediaList.length ? 'Selecione' : 'Nenhuma mídia pronta — envie abaixo'} />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  {uploaded && !mediaList.some((m) => m.id === uploaded.id) ? (
                    <SelectItem value={uploaded.id}>{uploaded.name} (recém-enviada)</SelectItem>
                  ) : null}
                  {mediaList.map((m) => (
                    <SelectItem key={m.id} value={m.id}>
                      {m.name} · {m.kind === 'VIDEO' ? `vídeo ${msToClock(m.output?.duration_ms)}` : 'imagem'} ·{' '}
                      {m.output?.orientation === 'VERTICAL' ? 'vertical' : 'horizontal'}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
            <FieldError>{errors.media}</FieldError>
          </Field>
          {chosenMedia ? <MediaPreview media={chosenMedia} className="max-h-48 w-full max-w-sm" /> : null}
          <details className="rounded-lg border border-border p-3">
            <summary className="min-h-11 cursor-pointer content-center text-sm font-semibold">Enviar nova mídia</summary>
            <MediaUploader
              orgId={orgId}
              onReady={(m) => {
                setUploaded(m)
                setMediaId(m.id)
                if (!name) setName(m.name.replace(/\.[^.]+$/, ''))
              }}
            />
          </details>
          <Field invalid={!!errors.name}>
            <FieldLabel required>Nome do anúncio</FieldLabel>
            <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={255} />
            <FieldError>{errors.name}</FieldError>
          </Field>
          <Field>
            <FieldLabel>Texto do anúncio (opcional)</FieldLabel>
            <Textarea value={adText} onChange={(e) => setAdText(e.target.value)} rows={2} maxLength={2000} />
            <FieldDescription>Ajuda a análise e a classificação. Após o envio para análise, mudanças exigem nova versão (novo código RD).</FieldDescription>
          </Field>
        </div>
      )}
      <ServerError error={serverError} />
      <div className="flex flex-wrap justify-end gap-3">
        <Button variant="secondary" onClick={onCancel} disabled={pending}>
          Cancelar
        </Button>
        <Button onClick={() => void submit()} loading={pending}>
          <Link2 aria-hidden="true" />
          Vincular anúncio
        </Button>
      </div>
    </section>
  )
}
