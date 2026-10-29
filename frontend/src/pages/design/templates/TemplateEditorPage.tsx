/**
 * Editor inicial de templates (`templates.manage`) — editor por FORMULÁRIO + pré-visualização:
 * grade 24×24, camadas texto/imagem/retângulo, cores dos tokens do DS, fontes do servidor,
 * 1920×1080 ou 1080×1920. Salvar: POST/PUT /templates (spec validada também no servidor).
 * Render oficial SOMENTE no servidor: POST /templates/{id}/preview (PNG/JPG) e
 * POST /templates/{id}/render com envio para a biblioteca de mídia (deliver_as_media).
 */
import * as React from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { ArrowDown, ArrowUp, Grid3x3, Image as ImageIcon, ImageUp, Plus, Save, Send, Square, Trash2, Type, Wand2 } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Checkbox } from '@/components/ui/checkbox'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Field, FieldDescription, FieldGroup, FieldLabel } from '@/components/ui/field'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ErrorState, NoPermission, PageSkeleton } from '@/components/patterns/states'
import { http } from '@/lib/http'
import { uploadWithProgress } from '@/lib/upload'
import { errorMessage, isApiError } from '@/lib/errors'
import { formatDateTime } from '@/lib/format'
import { cn } from '@/lib/utils'
import { fetchBinary } from '@/components/admin/binary'
import { useObjectUrl } from '@/components/admin/hooks'
import { OrgPicker, type PickedEntity } from '@/components/admin/pickers'
import { validateFile } from '@/components/patterns/file-validation'
import { TemplatePreview } from './TemplatePreview'
import {
  CANVAS,
  DEFAULT_FONTS,
  FONT_LABELS,
  GRID,
  TOKEN_COLORS,
  clampBox,
  emptyEditor,
  fromSpec,
  isTokenColor,
  newLayer,
  toSpec,
  validateSpec,
  type Box,
  type EditorLayer,
  type EditorState,
  type Orientation,
  type Variable,
} from './templateSpec'
import type { DesignTemplateRow, TemplateMeta } from '../types'

type Status = 'RASCUNHO' | 'ATIVO' | 'ARQUIVADO'

export default function TemplateEditorPage() {
  const { id } = useParams()
  const isNew = !id || id === 'novo'
  const meta = useQuery({ queryKey: ['design', 'templates', 'meta'], queryFn: () => http.get<TemplateMeta>('/templates/meta'), staleTime: Infinity })
  const tpl = useQuery({ queryKey: ['design', 'templates', 'detail', id], queryFn: () => http.get<DesignTemplateRow>(`/templates/${id}`), enabled: !isNew })
  if (meta.isLoading || (!isNew && tpl.isLoading)) return <PageSkeleton />
  if (isApiError(meta.error) && meta.error.status === 403) return <NoPermission permission="templates.manage" />
  if (!isNew && (tpl.error || !tpl.data)) return <ErrorState error={tpl.error} onRetry={() => void tpl.refetch()} />
  return <Editor key={tpl.data ? tpl.data.id : 'novo'} template={isNew ? null : tpl.data ?? null} fonts={meta.data?.fonts ?? [...DEFAULT_FONTS]} />
}

function Editor({ template, fonts }: { template: DesignTemplateRow | null; fonts: string[] }) {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [name, setName] = React.useState(template?.name ?? '')
  const [description, setDescription] = React.useState(template?.description ?? '')
  const [orientation, setOrientation] = React.useState<Orientation>((template?.orientation as Orientation) ?? 'HORIZONTAL')
  const [status, setStatus] = React.useState<Status>((template?.status as Status) ?? 'RASCUNHO')
  const [state, setState] = React.useState<EditorState>(() => (template ? fromSpec(template.spec) : emptyEditor()))
  const [selected, setSelected] = React.useState<string | null>(null)
  const [showGrid, setShowGrid] = React.useState(true)
  const [values, setValues] = React.useState<Record<string, string>>({})
  const [dirty, setDirty] = React.useState(!template)
  const [saving, setSaving] = React.useState(false)
  const [serverErrors, setServerErrors] = React.useState<string[]>([])
  const [rendered, setRendered] = React.useState<Blob | null>(null)
  const [rendering, setRendering] = React.useState(false)
  const [format, setFormat] = React.useState<'PNG' | 'JPEG'>('PNG')
  const [version, setVersion] = React.useState(template?.version ?? 0)
  const renderedUrl = useObjectUrl(rendered)

  const spec = toSpec(state)
  const problems = validateSpec(spec, fonts)
  const nameOk = name.trim().length >= 2
  const update = (fn: (s: EditorState) => EditorState) => {
    setState(fn)
    setDirty(true)
  }
  const updateLayer = (uid: string, patch: Partial<EditorLayer>) =>
    update((s) => ({ ...s, layers: s.layers.map((l) => (l.uid === uid ? ({ ...l, ...patch } as EditorLayer) : l)) }))

  const save = async (): Promise<DesignTemplateRow | null> => {
    setServerErrors([])
    if (!nameOk || problems.length) return null
    setSaving(true)
    try {
      let saved: DesignTemplateRow
      if (!template) {
        saved = await http.post<DesignTemplateRow>('/templates', { name: name.trim(), description: description.trim() || null, orientation, spec, status: status === 'ARQUIVADO' ? 'RASCUNHO' : status })
      } else {
        saved = await http.put<DesignTemplateRow>(`/templates/${template.id}`, {
          name: name.trim(),
          description: description.trim(),
          spec,
          status,
          expected_version: version,
        })
      }
      setVersion(saved.version)
      setDirty(false)
      await qc.invalidateQueries({ queryKey: ['design', 'templates'] })
      toast.success('Template salvo.')
      if (!template) navigate(`/design/templates/${saved.id}`, { replace: true })
      return saved
    } catch (e) {
      if (isApiError(e) && Array.isArray(e.details.errors)) {
        setServerErrors((e.details.errors as Array<{ loc?: string[]; msg?: string }>).map((x) => `${(x.loc ?? []).join('.')}: ${x.msg ?? ''}`))
      } else if (isApiError(e) && e.code === 'TEMPLATE_VERSION_CONFLICT') {
        setServerErrors(['O template foi alterado por outra pessoa. Recarregue a página para ver a versão atual.'])
      } else setServerErrors([errorMessage(e)])
      return null
    } finally {
      setSaving(false)
    }
  }

  const renderOnServer = async () => {
    if (!template || dirty) {
      const saved = await save()
      // Template novo: a página passa para o endereço dele; clique de novo para renderizar.
      if (!saved || !template) return
    }
    setRendering(true)
    try {
      const r = await fetchBinary(`/templates/${template.id}/preview`, { method: 'POST', body: { variables: variableValues(state.variables, values), format } })
      setRendered(r.blob)
    } catch (e) {
      toast.error(errorMessage(e))
    } finally {
      setRendering(false)
    }
  }

  const canvas = CANVAS[orientation]
  const sel = state.layers.find((l) => l.uid === selected) ?? null

  return (
    <>
      <PageHeader
        title={template ? `Template: ${template.name}` : 'Novo template'}
        description={template ? `Versão ${version} · atualizado ${formatDateTime(template.updated_at)}` : `${canvas.width}×${canvas.height}`}
        breadcrumbs={[{ label: 'Templates', to: '/design/templates' }, { label: template?.name ?? 'Novo template' }]}
        actions={
          <Button onClick={() => void save()} loading={saving} disabled={!nameOk || problems.length > 0 || (!dirty && !!template)}>
            <Save aria-hidden="true" />
            Salvar template
          </Button>
        }
      />
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-12 xl:gap-6">
        <div className="flex flex-col gap-4 xl:col-span-5">
          <Card>
            <CardHeader>
              <CardTitle>Dados do template</CardTitle>
            </CardHeader>
            <FieldGroup>
              <Field required invalid={!nameOk && name !== ''}>
                <FieldLabel>Nome</FieldLabel>
                <Input value={name} maxLength={120} onChange={(e) => { setName(e.target.value); setDirty(true) }} />
              </Field>
              <Field>
                <FieldLabel>Descrição</FieldLabel>
                <Textarea value={description} maxLength={2000} onChange={(e) => { setDescription(e.target.value); setDirty(true) }} />
              </Field>
              <Field>
                <FieldLabel id="tpl-ori">Formato da tela</FieldLabel>
                <ToggleGroup type="single" aria-labelledby="tpl-ori" value={orientation} onValueChange={(v) => v && setOrientation(v as Orientation)} disabled={!!template} className="w-fit">
                  <ToggleGroupItem value="HORIZONTAL">Horizontal 1920×1080</ToggleGroupItem>
                  <ToggleGroupItem value="VERTICAL">Vertical 1080×1920</ToggleGroupItem>
                </ToggleGroup>
                {template ? <FieldDescription>A orientação é definida na criação.</FieldDescription> : null}
              </Field>
              <Field>
                <FieldLabel id="tpl-status">Situação</FieldLabel>
                <ToggleGroup type="single" aria-labelledby="tpl-status" value={status} onValueChange={(v) => { if (v) { setStatus(v as Status); setDirty(true) } }} className="w-fit">
                  <ToggleGroupItem value="RASCUNHO">Rascunho</ToggleGroupItem>
                  <ToggleGroupItem value="ATIVO">Ativo</ToggleGroupItem>
                  {template ? <ToggleGroupItem value="ARQUIVADO">Arquivado</ToggleGroupItem> : null}
                </ToggleGroup>
                <FieldDescription>Só templates ativos aparecem para gerar versões de arte.</FieldDescription>
              </Field>
              <ColorField label="Cor de fundo" value={state.background} onChange={(c) => update((s) => ({ ...s, background: c }))} />
            </FieldGroup>
          </Card>

          <VariablesCard variables={state.variables} onChange={(vars) => update((s) => ({ ...s, variables: vars }))} />

          <Card>
            <CardHeader>
              <div className="flex flex-col gap-1">
                <CardTitle>Camadas</CardTitle>
                <CardDescription>Desenhadas de cima para baixo nesta ordem (a última fica por cima). Máximo de 30.</CardDescription>
              </div>
            </CardHeader>
            <div className="flex flex-wrap gap-2">
              <Button variant="secondary" disabled={state.layers.length >= 30} onClick={() => { const l = newLayer('text'); update((s) => ({ ...s, layers: [...s.layers, l] })); setSelected(l.uid) }}>
                <Type aria-hidden="true" />
                Texto
              </Button>
              <Button variant="secondary" disabled={state.layers.length >= 30} onClick={() => { const l = newLayer('image'); update((s) => ({ ...s, layers: [...s.layers, l] })); setSelected(l.uid) }}>
                <ImageIcon aria-hidden="true" />
                Imagem
              </Button>
              <Button variant="secondary" disabled={state.layers.length >= 30} onClick={() => { const l = newLayer('rect'); update((s) => ({ ...s, layers: [...s.layers, l] })); setSelected(l.uid) }}>
                <Square aria-hidden="true" />
                Retângulo
              </Button>
            </div>
            {state.layers.length ? (
              <ol className="m-0 flex list-none flex-col gap-1 p-0" aria-label="Camadas">
                {state.layers.map((l, i) => (
                  <li key={l.uid} className={cn('flex items-center gap-1 rounded-md border', selected === l.uid ? 'border-focus bg-surface-hover' : 'border-border')}>
                    <button
                      type="button"
                      className="flex min-h-11 min-w-0 flex-1 items-center gap-2 px-3 text-left text-sm"
                      aria-pressed={selected === l.uid}
                      onClick={() => setSelected(l.uid === selected ? null : l.uid)}
                    >
                      <span className="text-xs text-text-muted tabular-nums">{i + 1}</span>
                      <span className="font-semibold">{l.type === 'text' ? 'Texto' : l.type === 'image' ? 'Imagem' : 'Retângulo'}</span>
                      <span className="truncate text-text-muted">{l.type === 'text' ? l.text : `${l.box.col},${l.box.row} · ${l.box.col_span}×${l.box.row_span}`}</span>
                    </button>
                    <Button variant="ghost" size="icon" aria-label={`Subir camada ${i + 1}`} disabled={i === 0} onClick={() => update((s) => ({ ...s, layers: move(s.layers, i, i - 1) }))}>
                      <ArrowUp aria-hidden="true" />
                    </Button>
                    <Button variant="ghost" size="icon" aria-label={`Descer camada ${i + 1}`} disabled={i === state.layers.length - 1} onClick={() => update((s) => ({ ...s, layers: move(s.layers, i, i + 1) }))}>
                      <ArrowDown aria-hidden="true" />
                    </Button>
                    <Button variant="ghost" size="icon" aria-label={`Remover camada ${i + 1}`} onClick={() => update((s) => ({ ...s, layers: s.layers.filter((x) => x.uid !== l.uid) }))}>
                      <Trash2 aria-hidden="true" />
                    </Button>
                  </li>
                ))}
              </ol>
            ) : (
              <p className="text-sm text-text-muted">Adicione camadas de texto, imagem ou retângulo.</p>
            )}
            {sel ? <LayerEditor layer={sel} fonts={fonts} onChange={(patch) => updateLayer(sel.uid, patch)} /> : null}
          </Card>
        </div>

        <div className="flex flex-col gap-4 xl:col-span-7">
          <Card>
            <CardHeader>
              <div className="flex flex-col gap-1">
                <CardTitle>Pré-visualização aproximada</CardTitle>
                <CardDescription>Posições na grade {GRID}×{GRID} e margem de segurança de 5%. O resultado final é o renderizado pelo servidor.</CardDescription>
              </div>
              <Field orientation="horizontal" className="min-h-11 w-auto">
                <Checkbox checked={showGrid} onCheckedChange={(c) => setShowGrid(c === true)} />
                <FieldLabel className="font-normal">
                  <Grid3x3 aria-hidden="true" className="mr-1 inline size-4" />
                  Mostrar grade
                </FieldLabel>
              </Field>
            </CardHeader>
            <TemplatePreview state={state} orientation={orientation} values={values} showGrid={showGrid} selected={selected} />
          </Card>

          {problems.length || serverErrors.length ? (
            <Alert variant={serverErrors.length ? 'danger' : 'warning'}>
              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <AlertTitle>{serverErrors.length ? 'O servidor recusou o template' : 'Ajuste antes de salvar'}</AlertTitle>
                <AlertDescription className="text-text">
                  <ul className="m-0 flex list-disc flex-col gap-0.5 pl-5">
                    {[...serverErrors, ...problems].map((p) => (
                      <li key={p}>{p}</li>
                    ))}
                  </ul>
                </AlertDescription>
              </div>
            </Alert>
          ) : null}

          <Card>
            <CardHeader>
              <div className="flex flex-col gap-1">
                <CardTitle>Renderizar no servidor</CardTitle>
                <CardDescription>Gera a imagem oficial ({canvas.width}×{canvas.height}) com os valores de teste das variáveis. Alterações não salvas são salvas antes.</CardDescription>
              </div>
            </CardHeader>
            {state.variables.length ? (
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
                {state.variables.map((v) => (
                  <Field key={v.name || v.label}>
                    <FieldLabel>{v.label || v.name}</FieldLabel>
                    <Input value={values[v.name] ?? ''} placeholder={v.default ?? ''} maxLength={v.max_len} onChange={(e) => setValues((p) => ({ ...p, [v.name]: e.target.value }))} />
                  </Field>
                ))}
              </div>
            ) : null}
            <div className="flex flex-wrap items-center gap-3">
              <ToggleGroup type="single" aria-label="Formato da imagem" value={format} onValueChange={(v) => v && setFormat(v as 'PNG' | 'JPEG')}>
                <ToggleGroupItem value="PNG">PNG</ToggleGroupItem>
                <ToggleGroupItem value="JPEG">JPG</ToggleGroupItem>
              </ToggleGroup>
              <Button onClick={() => void renderOnServer()} loading={rendering || saving} disabled={!nameOk || problems.length > 0}>
                <Wand2 aria-hidden="true" />
                Renderizar no servidor
              </Button>
            </div>
            {renderedUrl ? (
              <figure className="m-0 flex flex-col gap-2">
                <img
                  src={renderedUrl}
                  alt={`Renderização do servidor do template ${name}`}
                  className={cn('w-full rounded-lg border border-border', orientation === 'VERTICAL' && 'mx-auto max-w-[360px]')}
                />
                <figcaption className="text-xs text-text-muted">
                  Renderizado pelo servidor · {format} · {canvas.width}×{canvas.height}
                </figcaption>
              </figure>
            ) : null}
          </Card>

          {template ? <DeliverCard template={template} disabled={dirty || problems.length > 0} variables={variableValues(state.variables, values)} format={format} /> : null}
        </div>
      </div>
    </>
  )
}

function move<T>(arr: T[], from: number, to: number): T[] {
  const next = [...arr]
  const [x] = next.splice(from, 1)
  next.splice(to, 0, x)
  return next
}

function variableValues(vars: Variable[], values: Record<string, string>): Record<string, string> {
  return Object.fromEntries(vars.filter((v) => v.name).map((v) => [v.name, values[v.name] ?? v.default ?? '']))
}

function ColorField({ label, value, onChange }: { label: string; value: string; onChange: (hex: string) => void }) {
  const offToken = !isTokenColor(value)
  return (
    <Field>
      <FieldLabel>{label}</FieldLabel>
      <Select value={value.toUpperCase()} onValueChange={onChange}>
        <SelectTrigger>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectGroup>
            {offToken ? <SelectItem value={value.toUpperCase()}>Cor fora dos tokens ({value.toUpperCase()})</SelectItem> : null}
            {TOKEN_COLORS.map((c) => (
              <SelectItem key={c.token} value={c.hex.toUpperCase()}>
                <span className="inline-flex items-center gap-2">
                  <span aria-hidden="true" className="inline-block size-4 rounded border border-border" style={{ background: c.hex }} />
                  {c.label} <code className="text-xs text-text-muted">{c.token}</code>
                </span>
              </SelectItem>
            ))}
          </SelectGroup>
        </SelectContent>
      </Select>
      {offToken ? <FieldDescription>Esta cor não é um token do design system; escolha um token para padronizar.</FieldDescription> : null}
    </Field>
  )
}

function NumberField({ label, value, min, max, onChange }: { label: string; value: number; min: number; max: number; onChange: (n: number) => void }) {
  return (
    <Field>
      <FieldLabel>{label}</FieldLabel>
      <Input
        type="number"
        inputMode="numeric"
        min={min}
        max={max}
        value={Number.isFinite(value) ? value : ''}
        onChange={(e) => onChange(e.target.value === '' ? min : Math.trunc(Number(e.target.value)))}
      />
    </Field>
  )
}

function BoxFields({ box, onChange }: { box: Box; onChange: (b: Box) => void }) {
  const set = (k: keyof Box, v: number) => onChange(clampBox({ ...box, [k]: v }))
  return (
    <fieldset className="m-0 grid grid-cols-2 gap-3 border-0 p-0 sm:grid-cols-4">
      <legend className="mb-2 text-sm font-semibold text-text">Posição na grade (24×24)</legend>
      <NumberField label="Coluna" value={box.col} min={0} max={GRID - 1} onChange={(v) => set('col', v)} />
      <NumberField label="Linha" value={box.row} min={0} max={GRID - 1} onChange={(v) => set('row', v)} />
      <NumberField label="Largura" value={box.col_span} min={1} max={GRID - box.col} onChange={(v) => set('col_span', v)} />
      <NumberField label="Altura" value={box.row_span} min={1} max={GRID - box.row} onChange={(v) => set('row_span', v)} />
    </fieldset>
  )
}

function LayerEditor({ layer, fonts, onChange }: { layer: EditorLayer; fonts: string[]; onChange: (patch: Partial<EditorLayer>) => void }) {
  return (
    <section aria-label="Editar camada selecionada" className="flex flex-col gap-4 rounded-lg border border-focus/60 p-4">
      <h3 className="text-title-card text-text">
        Editar {layer.type === 'text' ? 'texto' : layer.type === 'image' ? 'imagem' : 'retângulo'}
      </h3>
      <BoxFields box={layer.box} onChange={(box) => onChange({ box })} />
      {layer.type === 'text' ? (
        <>
          <Field>
            <FieldLabel>Texto</FieldLabel>
            <Textarea value={layer.text} maxLength={500} onChange={(e) => onChange({ text: e.target.value })} />
            <FieldDescription>Use variáveis declaradas no formato {'{{nome}}'}. Texto que não cabe é reduzido e cortado com “…”.</FieldDescription>
          </Field>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <Field>
              <FieldLabel>Fonte</FieldLabel>
              <Select value={layer.font} onValueChange={(font) => onChange({ font })}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    {fonts.map((f) => (
                      <SelectItem key={f} value={f}>
                        {FONT_LABELS[f] ?? f}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
            </Field>
            <NumberField label="Tamanho (px)" value={layer.size_px} min={12} max={400} onChange={(v) => onChange({ size_px: Math.min(400, Math.max(12, v)) })} />
            <ColorField label="Cor do texto" value={layer.color} onChange={(color) => onChange({ color })} />
            <NumberField label="Máximo de linhas" value={layer.max_lines} min={1} max={12} onChange={(v) => onChange({ max_lines: Math.min(12, Math.max(1, v)) })} />
          </div>
          <div className="flex flex-wrap gap-3">
            <ToggleGroup type="single" aria-label="Alinhamento horizontal" value={layer.align} onValueChange={(v) => v && onChange({ align: v as 'left' | 'center' | 'right' })}>
              <ToggleGroupItem value="left">Esquerda</ToggleGroupItem>
              <ToggleGroupItem value="center">Centro</ToggleGroupItem>
              <ToggleGroupItem value="right">Direita</ToggleGroupItem>
            </ToggleGroup>
            <ToggleGroup type="single" aria-label="Alinhamento vertical" value={layer.valign} onValueChange={(v) => v && onChange({ valign: v as 'top' | 'middle' | 'bottom' })}>
              <ToggleGroupItem value="top">Topo</ToggleGroupItem>
              <ToggleGroupItem value="middle">Meio</ToggleGroupItem>
              <ToggleGroupItem value="bottom">Base</ToggleGroupItem>
            </ToggleGroup>
          </div>
          <Field orientation="horizontal" className="min-h-11">
            <Checkbox checked={layer.uppercase} onCheckedChange={(c) => onChange({ uppercase: c === true })} />
            <FieldLabel className="font-normal">Tudo em maiúsculas</FieldLabel>
          </Field>
        </>
      ) : layer.type === 'image' ? (
        <>
          <AssetUpload assetId={layer.asset_id} onUploaded={(asset_id) => onChange({ asset_id })} />
          <ToggleGroup type="single" aria-label="Ajuste da imagem" value={layer.fit} onValueChange={(v) => v && onChange({ fit: v as 'contain' | 'cover' })} className="w-fit">
            <ToggleGroupItem value="contain">Caber inteira</ToggleGroupItem>
            <ToggleGroupItem value="cover">Preencher (corta)</ToggleGroupItem>
          </ToggleGroup>
        </>
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <ColorField label="Cor" value={layer.color} onChange={(color) => onChange({ color })} />
          <NumberField label="Opacidade (%)" value={layer.opacity} min={0} max={100} onChange={(v) => onChange({ opacity: Math.min(100, Math.max(0, v)) })} />
          <NumberField label="Arredondamento (px)" value={layer.radius_px} min={0} max={200} onChange={(v) => onChange({ radius_px: Math.min(200, Math.max(0, v)) })} />
        </div>
      )}
    </section>
  )
}

function AssetUpload({ assetId, onUploaded }: { assetId: string; onUploaded: (id: string) => void }) {
  const ref = React.useRef<HTMLInputElement>(null)
  const [progress, setProgress] = React.useState<number | null>(null)
  const [error, setError] = React.useState<string | null>(null)
  const upload = async (file: File) => {
    const problem = validateFile(file, ['image/png', 'image/jpeg'], 10 * 1024 * 1024)
    if (problem) {
      setError(`${problem} Use PNG ou JPG até 10 MB.`)
      return
    }
    setError(null)
    setProgress(0)
    try {
      const form = new FormData()
      form.append('file', file)
      const r = await uploadWithProgress<{ id: string }>({ path: '/templates/assets', form, onProgress: setProgress })
      onUploaded(r.id)
      toast.success('Imagem enviada.')
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setProgress(null)
    }
  }
  return (
    <div className="flex flex-col gap-2">
      <input
        ref={ref}
        type="file"
        accept="image/png,image/jpeg"
        className="sr-only"
        tabIndex={-1}
        aria-label="Imagem da camada"
        onChange={(e) => {
          const f = e.target.files?.[0]
          if (f) void upload(f)
          e.target.value = ''
        }}
      />
      <Button variant="secondary" className="w-fit" onClick={() => ref.current?.click()} loading={progress !== null} loadingText={`Enviando ${Math.round((progress ?? 0) * 100)}%`}>
        <ImageUp aria-hidden="true" />
        {assetId ? 'Trocar imagem' : 'Enviar imagem (PNG/JPG)'}
      </Button>
      {assetId ? <Badge tone="success" className="w-fit">Imagem enviada</Badge> : null}
      {error ? <p className="text-xs font-medium text-danger">{error}</p> : null}
    </div>
  )
}

function VariablesCard({ variables, onChange }: { variables: Variable[]; onChange: (v: Variable[]) => void }) {
  const set = (i: number, patch: Partial<Variable>) => onChange(variables.map((v, idx) => (idx === i ? { ...v, ...patch } : v)))
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Variáveis</CardTitle>
          <CardDescription>Campos preenchidos ao gerar a arte (ex.: preço, data). Até 20.</CardDescription>
        </div>
        <Button
          variant="secondary"
          disabled={variables.length >= 20}
          onClick={() => onChange([...variables, { name: `campo${variables.length + 1}`, label: `Campo ${variables.length + 1}`, max_len: 80, default: null }])}
        >
          <Plus aria-hidden="true" />
          Variável
        </Button>
      </CardHeader>
      {variables.length ? (
        <ul className="m-0 flex list-none flex-col gap-4 p-0">
          {variables.map((v, i) => (
            <li key={i} className="grid grid-cols-1 items-end gap-3 rounded-md border border-border p-3 sm:grid-cols-[1fr_1fr_100px_auto]">
              <Field>
                <FieldLabel>Nome ({'{{'}nome{'}}'})</FieldLabel>
                <Input value={v.name} onChange={(e) => set(i, { name: e.target.value.toLowerCase() })} spellCheck={false} />
              </Field>
              <Field>
                <FieldLabel>Rótulo</FieldLabel>
                <Input value={v.label} maxLength={60} onChange={(e) => set(i, { label: e.target.value })} />
              </Field>
              <Field>
                <FieldLabel>Máx.</FieldLabel>
                <Input type="number" min={1} max={300} value={v.max_len} onChange={(e) => set(i, { max_len: Math.trunc(Number(e.target.value) || 1) })} />
              </Field>
              <Button variant="ghost" size="icon" aria-label={`Remover variável ${v.label || v.name}`} onClick={() => onChange(variables.filter((_, idx) => idx !== i))}>
                <Trash2 aria-hidden="true" />
              </Button>
              <Field className="sm:col-span-4">
                <FieldLabel>Valor padrão</FieldLabel>
                <Input value={v.default ?? ''} maxLength={300} onChange={(e) => set(i, { default: e.target.value || null })} />
              </Field>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-text-muted">Nenhuma variável. Textos fixos não precisam de variáveis.</p>
      )}
    </Card>
  )
}

function DeliverCard({ template, disabled, variables, format }: { template: DesignTemplateRow; disabled: boolean; variables: Record<string, string>; format: 'PNG' | 'JPEG' }) {
  const [org, setOrg] = React.useState<PickedEntity | null>(null)
  const [mediaName, setMediaName] = React.useState(template.name)
  const [pending, setPending] = React.useState(false)
  const [result, setResult] = React.useState<{ media_id: string | null; media_status: string | null } | null>(null)
  const [error, setError] = React.useState<string | null>(null)
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Gerar e enviar para a biblioteca</CardTitle>
          <CardDescription>A imagem renderizada vira mídia da empresa escolhida, com o mesmo processamento do upload.</CardDescription>
        </div>
      </CardHeader>
      {disabled ? (
        <Alert variant="neutral">
          <AlertDescription>Salve o template (sem pendências) antes de gerar a mídia.</AlertDescription>
        </Alert>
      ) : null}
      <OrgPicker label="Empresa dona da mídia" value={org} onChange={setOrg} required />
      <Field>
        <FieldLabel>Nome da mídia</FieldLabel>
        <Input value={mediaName} maxLength={120} onChange={(e) => setMediaName(e.target.value)} />
      </Field>
      {error ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">{error}</AlertDescription>
        </Alert>
      ) : null}
      {result ? (
        <Alert variant="success">
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <AlertTitle>Mídia criada</AlertTitle>
            <AlertDescription>
              Situação: {result.media_status ?? '—'}.{' '}
              <Link to="/biblioteca" className="font-semibold text-link underline-offset-4 hover:underline">
                Abrir a biblioteca de mídia
              </Link>
            </AlertDescription>
          </div>
        </Alert>
      ) : null}
      <Button
        className="w-fit"
        disabled={disabled || !org}
        loading={pending}
        onClick={async () => {
          if (!org) return
          setPending(true)
          setError(null)
          try {
            const r = await http.post<{ media_id: string | null; media_status: string | null }>(`/templates/${template.id}/render`, {
              variables,
              format,
              org_id: org.id,
              deliver_as_media: true,
              name: mediaName.trim() || template.name,
            })
            setResult(r)
            toast.success('Arte gerada e enviada para a biblioteca.')
          } catch (e) {
            setError(errorMessage(e))
          } finally {
            setPending(false)
          }
        }}
      >
        <Send aria-hidden="true" />
        Gerar e enviar
      </Button>
    </Card>
  )
}
