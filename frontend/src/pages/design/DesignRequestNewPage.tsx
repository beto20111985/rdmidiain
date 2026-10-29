/**
 * Novo pedido de arte — POST /design/requests e anexos (POST …/attachments, com progresso
 * real). Briefing: objetivo, textos, referências, orientação, tipo de mídia e duração.
 * A criação da arte é um serviço pago à parte: a RD envia o orçamento e, quando houver
 * cobrança, o pedido só entra na fila após o pagamento (cobrança via financeiro).
 */
import * as React from 'react'
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Checkbox } from '@/components/ui/checkbox'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { FileDropzone, type UploadItem } from '@/components/patterns/file-dropzone'
import { useActiveContext, useActiveRouteContext } from '@/stores/contextStore'
import { http, newRequestId } from '@/lib/http'
import { uploadWithProgress } from '@/lib/upload'
import { errorMessage } from '@/lib/errors'
import { formatCents } from '@/lib/format'
import { EstablishmentPicker, OrgPicker, type PickedEntity } from '@/components/admin/pickers'
import { parseBrlToCents } from '@/components/admin/money'
import { DESIGN_PATHS } from './paths'
import { ATTACHMENT_ACCEPT, ATTACHMENT_MAX, attachmentProblem, inferAttachmentKind } from './attachments'
import type { DesignRequest } from './types'
import { zonedToUtcIso } from '@/lib/dates'

function lines(v: string): string[] {
  return v
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean)
}

export default function DesignRequestNewPage() {
  const ctx = useActiveRouteContext() ?? 'rd'
  const info = useActiveContext()
  const paths = DESIGN_PATHS[ctx]
  const navigate = useNavigate()
  const qc = useQueryClient()
  const staff = ctx === 'rd'

  const [org, setOrg] = React.useState<PickedEntity | null>(null)
  const [est, setEst] = React.useState<PickedEntity | null>(null)
  const [kind, setKind] = React.useState<'ANUNCIO_EXTERNO' | 'CONTEUDO_INTERNO'>(ctx === 'establishment' ? 'CONTEUDO_INTERNO' : 'ANUNCIO_EXTERNO')
  const [title, setTitle] = React.useState('')
  const [briefing, setBriefing] = React.useState('')
  const [texts, setTexts] = React.useState('')
  const [refs, setRefs] = React.useState('')
  const [orientation, setOrientation] = React.useState<'HORIZONTAL' | 'VERTICAL'>('HORIZONTAL')
  const [mediaType, setMediaType] = React.useState<'IMAGEM' | 'VIDEO'>('IMAGEM')
  const [duration, setDuration] = React.useState('30')
  const [due, setDue] = React.useState('')
  const [billing, setBilling] = React.useState<'A_DEFINIR' | 'COBRAR' | 'NEGOCIADO' | 'ISENTO'>('A_DEFINIR')
  const [price, setPrice] = React.useState('')
  const [asLogo, setAsLogo] = React.useState(false)
  const [items, setItems] = React.useState<Array<UploadItem & { kind: string }>>([])
  const [created, setCreated] = React.useState<DesignRequest | null>(null)
  const [submitted, setSubmitted] = React.useState(false)
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)

  const orgId = staff ? org?.id : info?.org_id
  const problems = {
    org: !orgId ? (staff ? 'Escolha a empresa cliente.' : 'Contexto sem empresa.') : null,
    est: kind === 'CONTEUDO_INTERNO' && !est ? 'Conteúdo interno exige o estabelecimento.' : null,
    title: title.trim().length < 3 ? 'Informe um título (mínimo 3 caracteres).' : null,
    briefing: briefing.trim().length < 10 ? 'Descreva o que você precisa (mínimo 10 caracteres).' : null,
    texts: lines(texts).length > 30 ? 'Máximo de 30 textos.' : null,
    refs: lines(refs).length > 30 ? 'Máximo de 30 referências.' : null,
    price: staff && billing === 'COBRAR' && !(parseBrlToCents(price) ?? 0) ? 'Informe o valor do serviço.' : null,
  }
  const invalid = Object.values(problems).some(Boolean)

  const uploadAll = async (req: DesignRequest, list: Array<UploadItem & { kind: string }>) => {
    let failures = 0
    for (const it of list) {
      if (it.status === 'ready') continue
      setItems((prev) => prev.map((p) => (p.id === it.id ? { ...p, status: 'uploading', progress: 0, error: undefined } : p)))
      try {
        const form = new FormData()
        form.append('kind', it.kind)
        form.append('file', it.file)
        await uploadWithProgress({
          path: `/design/requests/${req.id}/attachments`,
          form,
          onProgress: (f) => setItems((prev) => prev.map((p) => (p.id === it.id ? { ...p, progress: f } : p))),
        })
        setItems((prev) => prev.map((p) => (p.id === it.id ? { ...p, status: 'ready', progress: 1 } : p)))
      } catch (e) {
        failures++
        setItems((prev) => prev.map((p) => (p.id === it.id ? { ...p, status: 'error', error: errorMessage(e) } : p)))
      }
    }
    return failures
  }

  const submit = async () => {
    setSubmitted(true)
    setError(null)
    if (invalid || !orgId) return
    setPending(true)
    try {
      let req = created
      if (!req) {
        const body: Record<string, unknown> = {
          org_id: orgId,
          establishment_id: kind === 'CONTEUDO_INTERNO' || ctx === 'establishment' ? est?.id : undefined,
          kind,
          title: title.trim(),
          briefing: briefing.trim(),
          texts: lines(texts),
          references: lines(refs),
          orientation,
          media_type: mediaType,
          duration_s: Number(duration),
          due_at: due ? (zonedToUtcIso(due, '23:59:00') ?? undefined) : undefined,
        }
        if (staff && billing !== 'A_DEFINIR') {
          body.billing = billing
          body.price_cents = billing === 'COBRAR' ? parseBrlToCents(price) ?? 0 : 0
        }
        req = await http.post<DesignRequest>('/design/requests', body, { idempotencyKey: newRequestId() })
        setCreated(req)
      }
      const failures = await uploadAll(req, items)
      void qc.invalidateQueries({ queryKey: ['design'] })
      if (failures) {
        setError(`${failures} anexo(s) não foram enviados. Tente de novo ou siga para o pedido e anexe depois.`)
      } else {
        toast.success(`Pedido ${req.code} enviado.`)
        navigate(paths.detail(req.id), { replace: true })
      }
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setPending(false)
    }
  }

  return (
    <>
      <PageHeader title="Novo pedido de arte" breadcrumbs={[{ label: staff ? 'Briefings' : 'Design', to: paths.list }, { label: 'Novo pedido' }]} />
      <form
        noValidate
        className="mx-auto flex w-full max-w-4xl flex-col gap-4 xl:gap-6"
        onSubmit={(e) => {
          e.preventDefault()
          void submit()
        }}
      >
        <Alert variant="info">
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <AlertTitle>Serviço de criação pago à parte</AlertTitle>
            <AlertDescription>
              A RD analisa o briefing e informa o valor. Cada pedido inclui 3 revisões; a 4ª alteração é uma revisão extra, cobrada à parte. A arte
              só é publicada depois da sua aprovação formal.
            </AlertDescription>
          </div>
        </Alert>
        <Card>
          <CardHeader>
            <div className="flex flex-col gap-1">
              <CardTitle>Briefing</CardTitle>
              <CardDescription>Quanto mais detalhes, menos revisões serão necessárias.</CardDescription>
            </div>
          </CardHeader>
          <fieldset disabled={!!created} className="m-0 flex min-w-0 flex-col gap-5 border-0 p-0">
            <FieldGroup>
              {staff ? (
                <>
                  <OrgPicker label="Empresa cliente" value={org} onChange={(o) => { setOrg(o); setEst(null) }} required error={submitted ? problems.org ?? undefined : undefined} />
                  <Field>
                    <FieldLabel id="dr-kind">Tipo de arte</FieldLabel>
                    <ToggleGroup type="single" aria-labelledby="dr-kind" value={kind} onValueChange={(v) => v && setKind(v as typeof kind)} className="w-fit">
                      <ToggleGroupItem value="ANUNCIO_EXTERNO">Anúncio externo</ToggleGroupItem>
                      <ToggleGroupItem value="CONTEUDO_INTERNO">Conteúdo interno do estabelecimento</ToggleGroupItem>
                    </ToggleGroup>
                  </Field>
                </>
              ) : null}
              {kind === 'CONTEUDO_INTERNO' ? (
                <EstablishmentPicker
                  label="Estabelecimento"
                  orgId={orgId}
                  value={est}
                  onChange={setEst}
                  required
                  error={submitted ? problems.est ?? undefined : undefined}
                  description="O conteúdo interno é exibido nos blocos reservados deste estabelecimento."
                />
              ) : null}
              <Field required invalid={submitted && !!problems.title}>
                <FieldLabel>Título do pedido</FieldLabel>
                <Input value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} placeholder="Ex.: Promoção de inverno — café e pão de queijo" />
                <FieldError>{submitted ? problems.title : undefined}</FieldError>
              </Field>
              <Field required invalid={submitted && !!problems.briefing}>
                <FieldLabel>O que a arte precisa comunicar</FieldLabel>
                <Textarea value={briefing} maxLength={10000} onChange={(e) => setBriefing(e.target.value)} className="min-h-32" />
                <FieldDescription>Objetivo, público, oferta, tom, cores da marca e o que não pode faltar.</FieldDescription>
                <FieldError>{submitted ? problems.briefing : undefined}</FieldError>
              </Field>
              <Field invalid={!!problems.texts}>
                <FieldLabel>Textos que devem aparecer (um por linha)</FieldLabel>
                <Textarea value={texts} onChange={(e) => setTexts(e.target.value)} />
                <FieldDescription>Ex.: título, preço, telefone, endereço. Mensagens curtas leem melhor na TV.</FieldDescription>
                <FieldError>{problems.texts}</FieldError>
              </Field>
              <Field invalid={!!problems.refs}>
                <FieldLabel>Referências (uma por linha)</FieldLabel>
                <Textarea value={refs} onChange={(e) => setRefs(e.target.value)} />
                <FieldDescription>Links ou descrições de peças que você gosta.</FieldDescription>
                <FieldError>{problems.refs}</FieldError>
              </Field>
              <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
                <Field>
                  <FieldLabel id="dr-ori">Orientação da tela</FieldLabel>
                  <ToggleGroup type="single" aria-labelledby="dr-ori" value={orientation} onValueChange={(v) => v && setOrientation(v as typeof orientation)} className="w-fit">
                    <ToggleGroupItem value="HORIZONTAL">Horizontal 1920×1080</ToggleGroupItem>
                    <ToggleGroupItem value="VERTICAL">Vertical 1080×1920</ToggleGroupItem>
                  </ToggleGroup>
                </Field>
                <Field>
                  <FieldLabel id="dr-media">Formato</FieldLabel>
                  <ToggleGroup type="single" aria-labelledby="dr-media" value={mediaType} onValueChange={(v) => v && setMediaType(v as typeof mediaType)} className="w-fit">
                    <ToggleGroupItem value="IMAGEM">Imagem</ToggleGroupItem>
                    <ToggleGroupItem value="VIDEO">Vídeo</ToggleGroupItem>
                  </ToggleGroup>
                </Field>
                {mediaType === 'VIDEO' ? (
                  <Field>
                    <FieldLabel id="dr-dur">Duração</FieldLabel>
                    <ToggleGroup type="single" aria-labelledby="dr-dur" value={duration} onValueChange={(v) => v && setDuration(v)} className="w-fit">
                      <ToggleGroupItem value="30">30 s</ToggleGroupItem>
                      <ToggleGroupItem value="60">60 s</ToggleGroupItem>
                      <ToggleGroupItem value="120">120 s</ToggleGroupItem>
                    </ToggleGroup>
                  </Field>
                ) : null}
                <Field>
                  <FieldLabel>Prazo desejado</FieldLabel>
                  <Input type="date" value={due} onChange={(e) => setDue(e.target.value)} className="max-w-[200px]" />
                </Field>
              </div>
              {staff ? (
                <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
                  <Field>
                    <FieldLabel id="dr-bill">Orçamento</FieldLabel>
                    <ToggleGroup type="single" aria-labelledby="dr-bill" value={billing} onValueChange={(v) => v && setBilling(v as typeof billing)} className="w-fit">
                      <ToggleGroupItem value="A_DEFINIR">Definir depois</ToggleGroupItem>
                      <ToggleGroupItem value="COBRAR">Cobrar</ToggleGroupItem>
                      <ToggleGroupItem value="NEGOCIADO">Negociado</ToggleGroupItem>
                      <ToggleGroupItem value="ISENTO">Isento</ToggleGroupItem>
                    </ToggleGroup>
                  </Field>
                  {billing === 'COBRAR' ? (
                    <Field required invalid={submitted && !!problems.price}>
                      <FieldLabel>Valor da criação (R$)</FieldLabel>
                      <Input inputMode="decimal" value={price} onChange={(e) => setPrice(e.target.value)} className="max-w-[200px]" />
                      <FieldDescription>{parseBrlToCents(price) !== null ? `= ${formatCents(parseBrlToCents(price))}` : 'Use vírgula para os centavos.'}</FieldDescription>
                      <FieldError>{submitted ? problems.price : undefined}</FieldError>
                    </Field>
                  ) : null}
                </div>
              ) : null}
            </FieldGroup>
          </fieldset>
        </Card>
        <Card>
          <CardHeader>
            <div className="flex flex-col gap-1">
              <CardTitle>Arquivos do briefing</CardTitle>
              <CardDescription>Logotipo, imagens, vídeos, textos e referências.</CardDescription>
            </div>
          </CardHeader>
          <Field orientation="horizontal" className="min-h-11">
            <Checkbox checked={asLogo} onCheckedChange={(c) => setAsLogo(c === true)} />
            <FieldLabel className="font-normal">Marcar as próximas imagens como logotipo</FieldLabel>
          </Field>
          <FileDropzone
            accept={ATTACHMENT_ACCEPT}
            maxSizeBytes={ATTACHMENT_MAX}
            requirements="PNG, JPG, GIF, WEBP ou SVG até 10 MB · PDF até 20 MB · MP4/WEBM até 200 MB · TXT até 1 MB."
            items={items}
            disabled={pending}
            onFilesAccepted={(files) =>
              setItems((prev) => [
                ...prev,
                ...files.map((file) => {
                  const problem = attachmentProblem(file)
                  return {
                    id: newRequestId(),
                    file,
                    kind: inferAttachmentKind(file, asLogo),
                    status: (problem ? 'error' : 'selected') as UploadItem['status'],
                    error: problem ?? undefined,
                    meta: inferAttachmentKind(file, asLogo) === 'LOGOTIPO' ? 'logotipo' : undefined,
                  }
                }),
              ])
            }
            onRemove={(id) => setItems((prev) => prev.filter((p) => p.id !== id))}
            onRetry={(id) => setItems((prev) => prev.map((p) => (p.id === id && !attachmentProblem(p.file) ? { ...p, status: 'selected', error: undefined } : p)))}
          />
        </Card>
        {error ? (
          <Alert variant="danger">
            <AlertDescription className="text-text">{error}</AlertDescription>
          </Alert>
        ) : null}
        <div className="flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
          {created ? (
            <Button variant="secondary" onClick={() => navigate(paths.detail(created.id))}>
              Ir para o pedido {created.code}
            </Button>
          ) : (
            <Button variant="secondary" onClick={() => navigate(paths.list)} disabled={pending}>
              Cancelar
            </Button>
          )}
          <Button type="submit" loading={pending} disabled={items.some((i) => i.status === 'error' && !!attachmentProblem(i.file))}>
            {created ? 'Enviar anexos pendentes' : 'Enviar pedido'}
          </Button>
        </div>
      </form>
    </>
  )
}
