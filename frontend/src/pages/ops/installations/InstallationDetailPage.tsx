/**
 * Execução da instalação (técnico): iniciar, checklist, GPS do navegador (permissão explícita),
 * teste de conexão REAL (presença do player), fotos (JPEG/PNG ≤ 10 MB, câmera no celular),
 * assinatura do responsável (desenho ou imagem ≤ 2 MB) e conclusão (ou dispensa justificada do teste).
 * Backend: /installations/{id}/start|checklist|geolocation|connection-test|photos|signature|complete|cancel.
 */
import * as React from 'react'
import { useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Camera, CheckCircle2, Crosshair, Pencil, Play, PlugZap, Upload, XCircle } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Checkbox } from '@/components/ui/checkbox'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { ErrorState, NoPermission, PageSkeleton } from '@/components/patterns/states'
import { FileDropzone, type UploadItem } from '@/components/patterns/file-dropzone'
import { validateFile } from '@/components/patterns/file-validation'
import { DetailList } from '@/components/ops/detail-list'
import { EstablishmentName, ScreenCode, UserName } from '@/components/ops/entity-name'
import { GeoLink } from '@/components/ops/geo-link'
import { OptionSelect } from '@/components/ops/filter-select'
import { PresenceBadge } from '@/components/ops/presence-badge'
import { ReasonDialog } from '@/components/ops/reason-dialog'
import { SignaturePad, type SignaturePadHandle } from '@/components/ops/signature-pad'
import { useBrowserGeolocation } from '@/components/ops/use-geolocation'
import { useCan } from '@/hooks/usePermissions'
import { errorMessage, isApiError } from '@/lib/errors'
import { qk } from '@/lib/queryKeys'
import { formatBytes, formatDate, formatDateTime, formatNumber } from '@/lib/format'
import { installationsApi, presenceToSummary } from '@/pages/ops/api/client'
import { useEstablishmentLookup, useScreenPresence } from '@/pages/ops/api/hooks'
import {
  CONNECTION_TEST_LABEL,
  INSTALLATION_MISSING_LABEL,
  INSTALLATION_STATUS_LABEL,
  PHOTO_KIND_LABEL,
  labelOf,
} from '@/pages/ops/api/labels'
import type { ChecklistItem, Installation, InstallationPhoto } from '@/pages/ops/api/types'
import { connectionTestFresh, installationTone, PHOTO_ACCEPT, PHOTO_MAX_BYTES, SIGNATURE_MAX_BYTES } from './installationRules'
import { EditInstallationDialog } from './EditInstallationDialog'

function PhotoThumb({ inst, photo }: { inst: Installation; photo: InstallationPhoto }) {
  const [url, setUrl] = React.useState<string | null>(null)
  const [failed, setFailed] = React.useState(false)
  React.useEffect(() => {
    let revoked = false
    let objectUrl: string | null = null
    installationsApi
      .photoBlob(inst.id, photo.id)
      .then(({ blob }) => {
        if (revoked) return
        objectUrl = URL.createObjectURL(blob)
        setUrl(objectUrl)
      })
      .catch(() => setFailed(true))
    return () => {
      revoked = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [inst.id, photo.id])
  return (
    <figure className="m-0 flex flex-col gap-1">
      {url ? (
        <img src={url} alt={`Foto: ${labelOf(PHOTO_KIND_LABEL, photo.kind)}`} className="aspect-video w-full rounded-lg border border-border object-cover" />
      ) : (
        <div className="flex aspect-video w-full items-center justify-center rounded-lg border border-border bg-surface-hover text-xs text-text-muted">
          {failed ? 'Não foi possível carregar' : 'Carregando…'}
        </div>
      )}
      <figcaption className="text-xs text-text-muted">
        {labelOf(PHOTO_KIND_LABEL, photo.kind)} · {formatBytes(photo.size_bytes)}
        {photo.width ? ` · ${photo.width}×${photo.height}` : ''}
        {photo.uploaded_by_name ? ` · por ${photo.uploaded_by_name}` : ''}
      </figcaption>
    </figure>
  )
}

function Checklist({ inst, editable, onSaved }: { inst: Installation; editable: boolean; onSaved: (i: Installation) => void }) {
  const [items, setItems] = React.useState<ChecklistItem[]>(inst.checklist)
  React.useEffect(() => setItems(inst.checklist), [inst.checklist])
  const dirty = JSON.stringify(items) !== JSON.stringify(inst.checklist)
  const m = useMutation({
    mutationFn: () => installationsApi.checklist(inst.id, items.map((i) => ({ key: i.key, done: i.done, note: i.note }))),
    onSuccess: (r) => {
      onSaved(r)
      toast.success('Checklist salvo.')
    },
  })
  const pending = items.filter((i) => i.required && !i.done).length
  return (
    <div className="flex flex-col gap-3">
      <ul className="m-0 flex list-none flex-col gap-1 p-0">
        {items.map((it, idx) => (
          <li key={it.key} className="flex flex-col gap-1 rounded-lg border border-border p-2">
            <Field orientation="horizontal" className="min-h-11">
              <Checkbox
                checked={it.done}
                disabled={!editable}
                onCheckedChange={(c) => setItems((p) => p.map((x, i) => (i === idx ? { ...x, done: !!c } : x)))}
              />
              <FieldLabel className="font-normal">
                {it.label}
                {it.required ? <span className="ml-1 text-xs text-text-muted">(obrigatório)</span> : null}
              </FieldLabel>
            </Field>
            {editable ? (
              <Input
                aria-label={`Observação: ${it.label}`}
                placeholder="Observação (opcional)"
                value={it.note ?? ''}
                onChange={(e) => setItems((p) => p.map((x, i) => (i === idx ? { ...x, note: e.target.value || null } : x)))}
                maxLength={500}
              />
            ) : it.note ? (
              <span className="pl-8 text-xs text-text-muted">{it.note}</span>
            ) : null}
          </li>
        ))}
      </ul>
      <p className="m-0 text-sm text-text-muted">{pending ? `${pending} item(ns) obrigatório(s) pendente(s).` : 'Todos os itens obrigatórios concluídos.'}</p>
      {m.error ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">{errorMessage(m.error)}</AlertDescription>
        </Alert>
      ) : null}
      {editable ? (
        <Button className="w-fit" variant="secondary" onClick={() => m.mutate()} disabled={!dirty} loading={m.isPending}>
          Salvar checklist
        </Button>
      ) : null}
    </div>
  )
}

export default function InstallationDetailPage() {
  const { id = '' } = useParams()
  const can = useCan()
  const qc = useQueryClient()
  const key = qk.sub('maintenance', 'installations', 'detail', id)
  const q = useQuery({ queryKey: key, queryFn: () => installationsApi.get(id) })
  const inst = q.data
  const presence = useScreenPresence(inst?.screen_id ?? undefined)
  // horários no fuso do estabelecimento (nunca o do navegador); sem acesso → padrão da plataforma
  const tz = useEstablishmentLookup(inst?.establishment_id).data?.timezone ?? undefined
  const geo = useBrowserGeolocation()
  const sigRef = React.useRef<SignaturePadHandle>(null)
  const [sigEmpty, setSigEmpty] = React.useState(true)
  const [signer, setSigner] = React.useState('')
  const [signerRole, setSignerRole] = React.useState('')
  const [sigFile, setSigFile] = React.useState<File | null>(null)
  const [sigError, setSigError] = React.useState<string | null>(null)
  const [photoKind, setPhotoKind] = React.useState<string | null>('TV_WORKING')
  const [photos, setPhotos] = React.useState<UploadItem[]>([])
  const [waiver, setWaiver] = React.useState('')
  const [cancelOpen, setCancelOpen] = React.useState(false)
  const [editOpen, setEditOpen] = React.useState(false)
  const [missing, setMissing] = React.useState<string[]>([])
  const seq = React.useRef(0)

  const setData = (r: Installation) => {
    qc.setQueryData(key, (old: Installation | undefined) => ({ ...r, photos: r.photos ?? old?.photos }))
    void qc.invalidateQueries({ queryKey: qk.sub('maintenance', 'installations', 'list') })
  }
  const start = useMutation({ mutationFn: () => installationsApi.start(id), onSuccess: (r) => { setData(r); toast.success('Instalação iniciada.') } })
  const saveGeo = useMutation({
    mutationFn: async () => {
      const r = await geo.request()
      if (!r) throw new Error(geo.error ?? 'Localização não obtida.')
      return installationsApi.geolocation(id, r)
    },
    onSuccess: (r) => {
      setData(r)
      toast.success('Localização registrada.')
    },
  })
  const test = useMutation({
    mutationFn: () => installationsApi.connectionTest(id),
    onSuccess: (r) => {
      setData(r.installation)
      toast[r.result?.status === 'ONLINE' ? 'success' : 'warning'](labelOf(CONNECTION_TEST_LABEL, r.result?.status))
    },
  })
  const sign = useMutation({
    mutationFn: async () => {
      let blob: Blob | null = sigFile
      if (!blob) blob = (await sigRef.current?.toBlob()) ?? null
      if (!blob) throw new Error('Desenhe a assinatura ou envie uma imagem.')
      return installationsApi.signature(id, signer.trim(), signerRole.trim() || null, blob)
    },
    onSuccess: (r) => {
      setData(r)
      toast.success('Assinatura registrada.')
    },
  })
  const complete = useMutation({
    mutationFn: () => installationsApi.complete(id, waiver.trim() || null),
    onSuccess: (r) => {
      setMissing([])
      setData(r)
      void qc.invalidateQueries({ queryKey: ['screens'] })
      toast.success('Instalação concluída.')
    },
    onError: (e) => {
      if (isApiError(e) && e.code === 'INSTALLATION_INCOMPLETE') setMissing((e.details.missing as string[]) ?? [])
    },
  })
  const cancel = useMutation({ mutationFn: (reason: string) => installationsApi.cancel(id, reason), onSuccess: (r) => { setData(r); toast.success('Instalação cancelada.') } })

  const crumbs = React.useMemo(() => [{ label: 'Instalações', to: '/estoque/instalacoes' }, { label: 'Instalação' }], [])

  if (q.isLoading) return <PageSkeleton />
  if (isApiError(q.error) && q.error.isForbidden) return <NoPermission permission="equipment.view" />
  if (q.error || !inst) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />

  const working = inst.status === 'IN_PROGRESS' && can('equipment.manage')
  const editable = can('equipment.manage') && (inst.status === 'SCHEDULED' || inst.status === 'IN_PROGRESS')
  const ct = inst.connection_test
  const fresh = connectionTestFresh(ct)

  const uploadPhoto = async (item: UploadItem, kind: string) => {
    setPhotos((p) => p.map((i) => (i.id === item.id ? { ...i, status: 'uploading', progress: 0, error: undefined } : i)))
    try {
      await installationsApi.photo(id, kind, item.file, (f) => setPhotos((p) => p.map((i) => (i.id === item.id ? { ...i, progress: f } : i))))
      setPhotos((p) => p.map((i) => (i.id === item.id ? { ...i, status: 'ready', progress: 1 } : i)))
      void q.refetch()
    } catch (e) {
      setPhotos((p) => p.map((i) => (i.id === item.id ? { ...i, status: 'error', error: errorMessage(e) } : i)))
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Instalação"
        description={inst.scheduled_date ? `Agendada para ${formatDate(`${inst.scheduled_date}T12:00:00Z`)}` : 'Sem data agendada'}
        breadcrumbs={crumbs}
        actions={
          editable ? (
            <div className="flex flex-wrap gap-2">
              <Button variant="destructive" onClick={() => setCancelOpen(true)}>
                <XCircle aria-hidden="true" />
                Cancelar
              </Button>
              <Button variant="secondary" onClick={() => setEditOpen(true)}>
                <Pencil aria-hidden="true" />
                Editar agendamento
              </Button>
              {inst.status === 'SCHEDULED' ? (
                <Button onClick={() => start.mutate()} loading={start.isPending}>
                  <Play aria-hidden="true" />
                  Iniciar instalação
                </Button>
              ) : null}
            </div>
          ) : undefined
        }
      />
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={installationTone(inst.status)}>{labelOf(INSTALLATION_STATUS_LABEL, inst.status)}</Badge>
        {start.error ? <span className="text-sm text-danger">{errorMessage(start.error)}</span> : null}
      </div>
      {editable && !inst.screen_id ? (
        <Alert variant="warning">
          <AlertDescription className="flex flex-wrap items-center gap-3 text-text">
            Esta instalação ainda não tem tela vinculada; ela é obrigatória para concluir.
            <Button variant="secondary" onClick={() => setEditOpen(true)}>
              Vincular tela
            </Button>
          </AlertDescription>
        </Alert>
      ) : null}
      {inst.status === 'SCHEDULED' ? (
        <Alert variant="info">
          <AlertDescription className="text-text">Inicie a instalação no local para liberar checklist, GPS, fotos, assinatura e teste.</AlertDescription>
        </Alert>
      ) : null}
      <div className="grid grid-cols-1 gap-6 xl:grid-cols-12">
        <Card className="xl:col-span-5">
          <CardHeader>
            <CardTitle>Dados</CardTitle>
          </CardHeader>
          <DetailList
            columns={1}
            items={[
              { label: 'Estabelecimento', value: <EstablishmentName id={inst.establishment_id} name={inst.establishment_name} /> },
              { label: 'Tela', value: inst.screen_id ? <ScreenCode id={inst.screen_id} code={inst.screen_code} /> : 'Não vinculada' },
              { label: 'Técnico', value: <UserName id={inst.technician_user_id} name={inst.technician_name} /> },
              { label: 'Iniciada em', value: formatDateTime(inst.started_at, tz) },
              { label: 'Concluída em', value: formatDateTime(inst.completed_at, tz) },
              { label: 'Agendada por', value: inst.created_by ? <UserName id={inst.created_by} name={inst.created_by_name} /> : null },
              { label: 'Observações', value: inst.notes },
            ]}
          />
        </Card>
        <Card className="xl:col-span-7">
          <CardHeader>
            <CardTitle>Checklist</CardTitle>
          </CardHeader>
          <Checklist inst={inst} editable={working} onSaved={setData} />
        </Card>

        <Card className="xl:col-span-6">
          <CardHeader>
            <div className="flex flex-col gap-1">
              <CardTitle>Localização do técnico (GPS)</CardTitle>
              <CardDescription>Capturada pelo navegador somente quando você tocar no botão; o navegador pede permissão.</CardDescription>
            </div>
          </CardHeader>
          {inst.geolocation ? (
            <div className="flex flex-col gap-1">
              <GeoLink lat={inst.geolocation.lat} lng={inst.geolocation.lng} source="GPS" accuracyM={inst.geolocation.accuracy_m} />
              <span className="text-xs text-text-muted">
                Registrada em {formatDateTime(inst.geolocation.captured_at, tz)}
                {inst.geolocation.distance_to_establishment_m != null
                  ? ` · ${formatNumber(inst.geolocation.distance_to_establishment_m)} m do endereço cadastrado`
                  : ''}
              </span>
            </div>
          ) : (
            <p className="m-0 text-sm text-text-muted">Ainda não registrada.</p>
          )}
          {geo.error ? (
            <Alert variant="warning">
              <AlertDescription className="text-text">{geo.error}</AlertDescription>
            </Alert>
          ) : saveGeo.error && !geo.error ? (
            <Alert variant="danger">
              <AlertDescription className="text-text">{errorMessage(saveGeo.error)}</AlertDescription>
            </Alert>
          ) : null}
          {working ? (
            <Button className="w-fit" variant="secondary" onClick={() => saveGeo.mutate()} loading={saveGeo.isPending || geo.pending} loadingText="Obtendo localização…" disabled={!geo.supported}>
              <Crosshair aria-hidden="true" />
              {inst.geolocation ? 'Capturar novamente' : 'Capturar minha localização'}
            </Button>
          ) : null}
        </Card>

        <Card className="xl:col-span-6">
          <CardHeader>
            <div className="flex flex-col gap-1">
              <CardTitle>Teste de conexão</CardTitle>
              <CardDescription>Verifica no servidor se o player pareado está se comunicando agora (presença real).</CardDescription>
            </div>
            {inst.screen_id ? <PresenceBadge presence={presence.data ? presenceToSummary(presence.data) : undefined} loading={presence.isLoading} showLastContact /> : null}
          </CardHeader>
          {ct ? (
            <Alert variant={ct.status === 'ONLINE' ? 'success' : 'warning'}>
              <AlertTitle>{labelOf(CONNECTION_TEST_LABEL, ct.status)}</AlertTitle>
              <AlertDescription>
                Testado em {formatDateTime(ct.checked_at, tz)}
                {ct.app_version ? ` · app ${ct.app_version}` : ''}
                {ct.detail ? ` · ${ct.detail}` : ''}
                {ct.status === 'ONLINE' && !fresh ? ' · teste com mais de 30 min: repita antes de concluir.' : ''}
              </AlertDescription>
            </Alert>
          ) : (
            <p className="m-0 text-sm text-text-muted">Nenhum teste realizado.</p>
          )}
          {test.error ? (
            <Alert variant="danger">
              <AlertDescription className="text-text">{errorMessage(test.error)}</AlertDescription>
            </Alert>
          ) : null}
          {working ? (
            <Button className="w-fit" variant="secondary" onClick={() => test.mutate()} loading={test.isPending} disabled={!inst.screen_id}>
              <PlugZap aria-hidden="true" />
              Testar conexão agora
            </Button>
          ) : null}
          {!inst.screen_id ? (
            <p className="m-0 text-xs text-text-muted">
              Sem tela vinculada não há player para testar, e a conclusão exige a tela.
              {editable ? ' Vincule a tela em “Editar agendamento”.' : ' Peça à equipe RD para vincular a tela.'}
            </p>
          ) : null}
        </Card>

        <Card className="xl:col-span-12">
          <CardHeader>
            <div className="flex flex-col gap-1">
              <CardTitle>Fotos</CardTitle>
              <CardDescription>Obrigatória: “TV funcionando”. JPEG ou PNG até 10 MB; no celular, use a câmera.</CardDescription>
            </div>
          </CardHeader>
          {(inst.photos ?? []).length ? (
            <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
              {(inst.photos ?? []).map((p) => (
                <PhotoThumb key={p.id} inst={inst} photo={p} />
              ))}
            </div>
          ) : (
            <p className="m-0 text-sm text-text-muted">Nenhuma foto enviada.</p>
          )}
          {working ? (
            <div className="flex flex-col gap-4">
              <Field className="max-w-xs">
                <FieldLabel>Tipo da foto</FieldLabel>
                <OptionSelect value={photoKind} onChange={setPhotoKind} options={Object.entries(PHOTO_KIND_LABEL).map(([value, label]) => ({ value, label }))} />
              </Field>
              <FileDropzone
                accept={PHOTO_ACCEPT}
                maxSizeBytes={PHOTO_MAX_BYTES}
                label="Selecionar fotos"
                requirements="JPEG ou PNG, até 10 MB por foto (máximo de 30 fotos por instalação)."
                items={photos}
                onFilesAccepted={(files) => {
                  const kind = photoKind ?? 'OTHER'
                  const created = files.map((file) => ({ id: `ph-${++seq.current}`, file, status: 'selected' as const }))
                  setPhotos((p) => [...p, ...created])
                  created.forEach((it) => void uploadPhoto(it, kind))
                }}
                onRetry={(pid) => {
                  const it = photos.find((x) => x.id === pid)
                  if (it) void uploadPhoto(it, photoKind ?? 'OTHER')
                }}
                onRemove={(pid) => setPhotos((p) => p.filter((x) => x.id !== pid))}
              />
              <label className="inline-flex min-h-11 w-fit cursor-pointer items-center gap-2 rounded-md border border-control-border px-4 text-sm font-semibold text-text hover:bg-surface-hover focus-within:outline focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-[rgb(var(--focus))]">
                <Camera aria-hidden="true" className="size-[18px]" />
                Tirar foto com a câmera
                <input
                  type="file"
                  accept="image/jpeg,image/png"
                  capture="environment"
                  className="sr-only"
                  onChange={(e) => {
                    const file = e.target.files?.[0]
                    e.target.value = ''
                    if (!file) return
                    const reason = validateFile(file, PHOTO_ACCEPT, PHOTO_MAX_BYTES)
                    if (reason) {
                      toast.error(`${file.name}: ${reason}`)
                      return
                    }
                    const it = { id: `ph-${++seq.current}`, file, status: 'selected' as const }
                    setPhotos((p) => [...p, it])
                    void uploadPhoto(it, photoKind ?? 'OTHER')
                  }}
                />
              </label>
            </div>
          ) : null}
        </Card>

        <Card className="xl:col-span-12">
          <CardHeader>
            <CardTitle>Assinatura do responsável</CardTitle>
          </CardHeader>
          {inst.signer ? (
            <p className="m-0 text-sm text-text">
              Assinado por <strong>{inst.signer.name}</strong>
              {inst.signer.role ? ` (${inst.signer.role})` : ''} em {formatDateTime(inst.signer.signed_at, tz)}.
            </p>
          ) : (
            <p className="m-0 text-sm text-text-muted">Ainda não assinado.</p>
          )}
          {working ? (
            <form
              noValidate
              className="flex flex-col gap-4"
              onSubmit={(e) => {
                e.preventDefault()
                if (signer.trim().length < 3) {
                  setSigError('Informe o nome de quem assina (mínimo 3 caracteres).')
                  return
                }
                setSigError(null)
                sign.mutate()
              }}
            >
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <Field invalid={!!sigError} required>
                  <FieldLabel>Nome de quem assina</FieldLabel>
                  <Input value={signer} onChange={(e) => setSigner(e.target.value)} autoComplete="off" />
                  <FieldError>{sigError}</FieldError>
                </Field>
                <Field>
                  <FieldLabel>Função</FieldLabel>
                  <Input value={signerRole} onChange={(e) => setSignerRole(e.target.value)} placeholder="Ex.: gerente" />
                </Field>
              </div>
              {!sigFile ? <SignaturePad ref={sigRef} label="Área de assinatura" onChange={setSigEmpty} /> : null}
              <Field>
                <FieldLabel>Ou envie uma imagem da assinatura</FieldLabel>
                <Input
                  type="file"
                  accept="image/jpeg,image/png"
                  onChange={(e) => {
                    const f = e.target.files?.[0] ?? null
                    if (f) {
                      const reason = validateFile(f, PHOTO_ACCEPT, SIGNATURE_MAX_BYTES)
                      if (reason) {
                        toast.error(`${f.name}: ${reason}`)
                        e.target.value = ''
                        return
                      }
                    }
                    setSigFile(f)
                  }}
                />
                <FieldDescription>JPEG ou PNG até 2 MB (alternativa acessível ao desenho).</FieldDescription>
              </Field>
              {sign.error ? (
                <Alert variant="danger">
                  <AlertDescription className="text-text">{errorMessage(sign.error)}</AlertDescription>
                </Alert>
              ) : null}
              <Button type="submit" className="w-fit" variant="secondary" loading={sign.isPending} disabled={sigEmpty && !sigFile}>
                <Upload aria-hidden="true" />
                Registrar assinatura
              </Button>
            </form>
          ) : null}
        </Card>

        {working ? (
          <Card className="xl:col-span-12">
            <CardHeader>
              <CardTitle>Concluir instalação</CardTitle>
            </CardHeader>
            {!fresh ? (
              <Field>
                <FieldLabel>Dispensa do teste de conexão (se o player não puder ficar online agora)</FieldLabel>
                <Textarea value={waiver} onChange={(e) => setWaiver(e.target.value)} rows={2} maxLength={2000} />
                <FieldDescription>Sem teste ONLINE nos últimos 30 min, é obrigatória uma justificativa com 20+ caracteres (auditada).</FieldDescription>
              </Field>
            ) : (
              <p className="m-0 flex items-center gap-2 text-sm text-success">
                <CheckCircle2 aria-hidden="true" className="size-4" /> Teste de conexão ONLINE recente.
              </p>
            )}
            {missing.length ? (
              <Alert variant="danger">
                <AlertTitle>Faltam itens para concluir</AlertTitle>
                <AlertDescription>
                  <ul className="m-0 list-disc pl-5">
                    {missing.map((m) => (
                      <li key={m}>
                        {m.startsWith('checklist:') ? `Checklist obrigatório: ${m.slice(10).split(',').length} item(ns)` : labelOf(INSTALLATION_MISSING_LABEL, m)}
                      </li>
                    ))}
                  </ul>
                </AlertDescription>
              </Alert>
            ) : complete.error ? (
              <Alert variant="danger">
                <AlertDescription className="text-text">{errorMessage(complete.error)}</AlertDescription>
              </Alert>
            ) : null}
            <Button className="w-fit" onClick={() => complete.mutate()} loading={complete.isPending}>
              <CheckCircle2 aria-hidden="true" />
              Concluir instalação
            </Button>
          </Card>
        ) : null}
      </div>
      <EditInstallationDialog
        inst={inst}
        open={editOpen}
        onOpenChange={setEditOpen}
        onSaved={(r) => {
          setData(r)
          toast.success('Agendamento atualizado.')
        }}
      />
      <ReasonDialog
        open={cancelOpen}
        onOpenChange={setCancelOpen}
        title="Cancelar esta instalação?"
        consequences={['O registro fica no histórico com o motivo informado.']}
        confirmLabel="Cancelar instalação"
        cancelLabel="Voltar"
        destructive
        onConfirm={(reason) => cancel.mutateAsync(reason)}
      />
    </div>
  )
}
