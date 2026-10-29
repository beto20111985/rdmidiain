/**
 * Etapa 1 — Identificação: nome, descrição, anunciante (equipe RD), plano, classificação
 * do anúncio (produto/marca/segmentos — base do motor de conflitos) e pedido de arte.
 */
import * as React from 'react'
import { Link } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Ban, Palette } from 'lucide-react'
import { toast } from 'sonner'
import { Field, FieldDescription, FieldError, FieldLabel, FieldLegend, FieldSet } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { Checkbox } from '@/components/ui/checkbox'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { EntityPicker, type PickerOption } from '@/components/campaigns/entity-picker'
import { qk } from '@/lib/queryKeys'
import { formatBps, formatCents } from '@/lib/format'
import { campaignsApi, pricingApi, taxonomyApi } from '../lib/api'
import { PERIOD_LABEL } from '../lib/labels'
import { searchBrands, searchOrganizations, searchProducts } from '../lib/pickers'
import { serverFieldErrors } from '../lib/rules'
import { treeOrder } from '../lib/tree'
import { ServerError, StepActions, type StepProps } from './shared'

export function StepIdentification({ campaign, area, onSaved, readOnly }: StepProps) {
  const qc = useQueryClient()
  const [name, setName] = React.useState(campaign?.name ?? '')
  const [description, setDescription] = React.useState(campaign?.description ?? '')
  const [org, setOrg] = React.useState<PickerOption | null>(null)
  const [planId, setPlanId] = React.useState<string>(campaign?.plan_id ?? '')
  const [art, setArt] = React.useState(campaign?.request_art_service ?? false)
  const [product, setProduct] = React.useState<PickerOption | null>(campaign?.product_id ? { id: campaign.product_id, label: 'Produto selecionado' } : null)
  const [brand, setBrand] = React.useState<PickerOption | null>(campaign?.brand_id ? { id: campaign.brand_id, label: 'Marca selecionada' } : null)
  const [segments, setSegments] = React.useState<string[]>(campaign?.segment_ids ?? [])
  const [segFilter, setSegFilter] = React.useState('')
  const [errors, setErrors] = React.useState<Record<string, string>>({})
  const [serverError, setServerError] = React.useState<unknown>(null)
  const [saving, setSaving] = React.useState<'draft' | 'next' | null>(null)

  const orgId = campaign?.org_id ?? (area.isStaff ? org?.id : area.contextOrgId) ?? null
  const plans = useQuery({
    queryKey: qk.sub('pricing', 'catalog', orgId ?? 'none'),
    queryFn: () => pricingApi.catalog(orgId),
    enabled: !area.isStaff || !!orgId,
  })
  const segQ = useQuery({ queryKey: qk.sub('taxonomy', 'segments'), queryFn: () => taxonomyApi.segments(), staleTime: 10 * 60 * 1000 })
  const segs = treeOrder(segQ.data?.items ?? [])
  const shownSegs = segFilter ? segs.filter((s) => s.name.toLowerCase().includes(segFilter.toLowerCase())) : segs

  const validate = () => {
    const e: Record<string, string> = {}
    if (name.trim().length < 3) e.name = 'O nome precisa ter ao menos 3 caracteres.'
    if (area.isStaff && !campaign && !org) e.org = 'Escolha o anunciante dono da campanha.'
    setErrors(e)
    return Object.keys(e).length === 0
  }

  const save = async (advance: boolean) => {
    if (readOnly || !validate()) return
    setSaving(advance ? 'next' : 'draft')
    setServerError(null)
    try {
      const body = {
        name: name.trim(),
        description: description.trim() || null,
        plan_id: planId || null,
        request_art_service: art,
        product_id: product?.id ?? null,
        brand_id: brand?.id ?? null,
      }
      let saved
      if (!campaign) {
        saved = await campaignsApi.create({ ...body, ...(area.isStaff && org ? { org_id: org.id } : {}), segment_ids: segments })
        toast.success('Rascunho criado.')
      } else {
        saved = await campaignsApi.update(campaign.id, { ...body, expected_version: campaign.version })
        const before = [...(campaign.segment_ids ?? [])].sort().join()
        if (before !== [...segments].sort().join()) {
          await campaignsApi.setSegments(campaign.id, segments)
          // segmentos incrementam a versão: recarrega para manter o expected_version correto
          saved = await campaignsApi.get(campaign.id)
        }
        toast.success('Rascunho salvo.')
      }
      qc.setQueryData(qk.detail('campaigns', saved.id), saved)
      void qc.invalidateQueries({ queryKey: qk.root('campaigns') })
      onSaved(saved, advance)
    } catch (e) {
      const fe = serverFieldErrors(e)
      if (Object.keys(fe).length) setErrors((prev) => ({ ...prev, ...fe }))
      setServerError(e)
    } finally {
      setSaving(null)
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <Field invalid={!!errors.name} required>
          <FieldLabel required>Nome da campanha</FieldLabel>
          <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={255} disabled={readOnly} />
          <FieldError>{errors.name}</FieldError>
        </Field>
        {area.isStaff && !campaign ? (
          <EntityPicker
            label="Anunciante"
            domain="organizations"
            value={org}
            onChange={setOrg}
            required
            error={errors.org}
            search={(t) => searchOrganizations(t, 'ADVERTISER')}
          />
        ) : null}
      </div>
      <Field>
        <FieldLabel>Descrição</FieldLabel>
        <Textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={3} maxLength={5000} disabled={readOnly} />
      </Field>

      <Field>
        <FieldLabel>Plano</FieldLabel>
        <Select value={planId || '__none'} onValueChange={(v) => setPlanId(v === '__none' ? '' : v)} disabled={readOnly || (area.isStaff && !orgId)}>
          <SelectTrigger>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              <SelectItem value="__none">Sem plano (preço pela tabela vigente)</SelectItem>
              {(plans.data?.items ?? []).map((p) => (
                <SelectItem key={p.id} value={p.id}>
                  {p.name} · {PERIOD_LABEL[p.period_type]} · {p.duration_s} s
                  {p.discount_bps ? ` · desconto ${formatBps(p.discount_bps, 0)}` : ''}
                  {p.reference_total_cents ? ` · referência ${formatCents(p.reference_total_cents)}` : ''}
                </SelectItem>
              ))}
            </SelectGroup>
          </SelectContent>
        </Select>
        <FieldDescription>O plano preenche período, duração e frequência padrão; o preço final é sempre calculado pelo servidor na revisão.</FieldDescription>
      </Field>

      <FieldSet>
        <FieldLegend>Classificação do anúncio</FieldLegend>
        <p className="m-0 text-xs text-text-muted">
          Usada para evitar concorrentes no mesmo estabelecimento e para aplicar a política de conteúdo (apostas, conteúdo adulto e armas são proibidos).
        </p>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <EntityPicker label="Produto" domain="taxonomy" value={product} onChange={setProduct} disabled={readOnly} search={searchProducts} />
          <EntityPicker label="Marca" domain="taxonomy" value={brand} onChange={setBrand} disabled={readOnly} search={searchBrands} />
        </div>
        <Field>
          <FieldLabel>Filtrar segmentos</FieldLabel>
          <Input type="search" value={segFilter} onChange={(e) => setSegFilter(e.target.value)} />
        </Field>
        <div className="max-h-64 overflow-y-auto rounded-lg border border-border p-2">
          {segQ.isLoading ? (
            <p className="m-0 p-2 text-sm text-text-muted">Carregando segmentos…</p>
          ) : shownSegs.length === 0 ? (
            <p className="m-0 p-2 text-sm text-text-muted">Nenhum segmento.</p>
          ) : (
            shownSegs.map((s) => (
              <Field key={s.id} orientation="horizontal" className="min-h-11" style={{ paddingLeft: `${s.depth * 16}px` }}>
                <Checkbox
                  checked={segments.includes(s.id)}
                  disabled={readOnly || s.policy === 'BLOCK'}
                  onCheckedChange={(c) => setSegments((prev) => (c ? [...prev, s.id] : prev.filter((x) => x !== s.id)))}
                />
                <FieldLabel className="font-normal">{s.name}</FieldLabel>
                {s.policy === 'BLOCK' ? (
                  <Badge tone="danger">
                    <Ban aria-hidden="true" />
                    Conteúdo proibido
                  </Badge>
                ) : null}
              </Field>
            ))
          )}
        </div>
      </FieldSet>

      <div className="flex flex-col gap-2">
        <Field orientation="horizontal" className="min-h-11">
          <Switch checked={art} onCheckedChange={setArt} disabled={readOnly} />
          <FieldLabel className="font-normal">Contratar criação de arte pela RD (taxa de serviço na cotação)</FieldLabel>
        </Field>
        <Button variant="tertiary" asChild className="w-fit">
          <Link to={area.designPath}>
            <Palette aria-hidden="true" />
            Abrir solicitações de design
          </Link>
        </Button>
      </div>

      <ServerError error={serverError} />
      {!readOnly ? <StepActions saving={saving} onSave={(a) => void save(a)} /> : null}
    </div>
  )
}
