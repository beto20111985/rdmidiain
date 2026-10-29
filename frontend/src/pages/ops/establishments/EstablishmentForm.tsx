/**
 * Formulário de estabelecimento (criar/editar) — POST /establishments e PATCH /establishments/{id}.
 * Fuso IANA: sugestão do servidor por UF/cidade (GET /establishments/timezone-suggestion), corrigível.
 * Coordenadas na criação (manual ou GPS do navegador com permissão explícita); na edição, pelo detalhe (PUT /geo).
 */
import * as React from 'react'
import { useNavigate } from 'react-router-dom'
import { useForm, Controller } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Crosshair, Wand2 } from 'lucide-react'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Switch } from '@/components/ui/switch'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { OptionSelect } from '@/components/ops/filter-select'
import { OrganizationPicker, TechnicianSelect } from '@/components/ops/pickers'
import { useBrowserGeolocation } from '@/components/ops/use-geolocation'
import { useDebounced } from '@/components/ops/use-debounced'
import { qk } from '@/lib/queryKeys'
import { treeOrder } from '@/lib/tree'
import { establishmentsApi } from '@/pages/ops/api/client'
import { usePartnerPlans, useTaxonomyNodes } from '@/pages/ops/api/hooks'
import { applyServerErrors } from '@/pages/ops/api/form-errors'
import { UFS } from '@/pages/ops/api/labels'
import type { Establishment } from '@/pages/ops/api/types'
import { establishmentSchema, FIELDS, toApiBody, toFormValues, type EstablishmentFormValues } from './establishmentFormModel'

export function EstablishmentForm({ establishment }: { establishment: Establishment | null }) {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const nodes = useTaxonomyNodes()
  const plans = usePartnerPlans(false)
  const geo = useBrowserGeolocation()
  const [generalError, setGeneralError] = React.useState<string | null>(null)
  const isEdit = !!establishment

  const {
    register,
    handleSubmit,
    control,
    setValue,
    setError,
    watch,
    formState: { errors, isSubmitting },
  } = useForm<EstablishmentFormValues>({
    resolver: zodResolver(establishmentSchema),
    defaultValues: toFormValues(establishment),
  })

  const state = watch('state')
  const city = watch('city')
  const tz = watch('timezone')
  const paysFee = watch('pays_monthly_fee')
  const debCity = useDebounced(city, 500)
  const suggestion = useQuery({
    queryKey: qk.sub('establishments', 'timezone-suggestion', state, debCity.trim()),
    queryFn: () => establishmentsApi.timezoneSuggestion(state, debCity.trim() || undefined),
    enabled: state.length === 2,
    staleTime: Infinity,
    retry: false,
  })

  const mutation = useMutation({
    mutationFn: (v: EstablishmentFormValues) =>
      isEdit ? establishmentsApi.update(establishment!.id, toApiBody(v)) : establishmentsApi.create(toApiBody(v)),
    onSuccess: (est) => {
      void qc.invalidateQueries({ queryKey: ['establishments'] })
      toast.success(isEdit ? 'Estabelecimento atualizado.' : 'Estabelecimento cadastrado.')
      navigate(`/estabelecimentos/${est.id}`)
    },
  })

  const onSubmit = async (v: EstablishmentFormValues) => {
    setGeneralError(null)
    try {
      await mutation.mutateAsync(v)
    } catch (err) {
      setGeneralError(
        applyServerErrors(err, setError, {
          fields: FIELDS,
          codeToField: {
            TIMEZONE_INVALID: 'timezone',
            DOCUMENT_INVALID: 'document',
            STATE_INVALID: 'state',
            POSTAL_CODE_INVALID: 'postal_code',
            CATEGORY_INVALID: 'category_id',
            PARTNER_PLAN_INVALID: 'partner_plan_id',
            TECHNICIAN_OUT_OF_SCOPE: 'responsible_technician_id',
          },
        }),
      )
    }
  }

  const captureGps = async () => {
    const r = await geo.request()
    if (!r) return
    setValue('lat', String(r.lat), { shouldValidate: true })
    setValue('lng', String(r.lng), { shouldValidate: true })
    setValue('geo_source', 'GPS')
    setValue('geo_accuracy_m', r.accuracy_m)
    toast.success('Localização capturada pelo GPS deste dispositivo.')
  }

  return (
    <form onSubmit={handleSubmit(onSubmit)} noValidate className="flex flex-col gap-6">
      {generalError ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">{generalError}</AlertDescription>
        </Alert>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Identificação</CardTitle>
        </CardHeader>
        <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
          {!isEdit ? (
            <div className="md:col-span-2">
              <Controller
                control={control}
                name="org_id"
                render={({ field }) => (
                  <OrganizationPicker
                    required
                    value={field.value}
                    onChange={(id) => field.onChange(id)}
                    error={errors.org_id?.message}
                    description="Empresa dona do estabelecimento (papel Parceiro). Uma empresa pode ter várias unidades."
                  />
                )}
              />
            </div>
          ) : null}
          <Field invalid={!!errors.name} required>
            <FieldLabel>Nome do estabelecimento</FieldLabel>
            <Input {...register('name')} autoComplete="organization" />
            <FieldError>{errors.name?.message}</FieldError>
          </Field>
          <Field invalid={!!errors.trade_name}>
            <FieldLabel>Nome fantasia</FieldLabel>
            <Input {...register('trade_name')} />
            <FieldError>{errors.trade_name?.message}</FieldError>
          </Field>
          <Field invalid={!!errors.legal_name}>
            <FieldLabel>Razão social</FieldLabel>
            <Input {...register('legal_name')} />
            <FieldError>{errors.legal_name?.message}</FieldError>
          </Field>
          <div className="grid grid-cols-1 gap-5 sm:grid-cols-[140px_1fr]">
            <Field invalid={!!errors.document_type}>
              <FieldLabel>Tipo</FieldLabel>
              <Controller
                control={control}
                name="document_type"
                render={({ field }) => (
                  <OptionSelect
                    value={field.value}
                    onChange={(v) => field.onChange(v)}
                    includeNone
                    noneLabel="—"
                    options={[
                      { value: 'CNPJ', label: 'CNPJ' },
                      { value: 'CPF', label: 'CPF' },
                    ]}
                  />
                )}
              />
              <FieldError>{errors.document_type?.message}</FieldError>
            </Field>
            <Field invalid={!!errors.document}>
              <FieldLabel>CPF/CNPJ</FieldLabel>
              <Input {...register('document')} inputMode="text" autoComplete="off" />
              <FieldDescription>CNPJ alfanumérico (2026) aceito. Dígitos verificados pelo servidor.</FieldDescription>
              <FieldError>{errors.document?.message}</FieldError>
            </Field>
          </div>
          <Field invalid={!!errors.responsible_name}>
            <FieldLabel>Responsável</FieldLabel>
            <Input {...register('responsible_name')} autoComplete="name" />
            <FieldError>{errors.responsible_name?.message}</FieldError>
          </Field>
          <Field invalid={!!errors.phone}>
            <FieldLabel>Telefone</FieldLabel>
            <Input {...register('phone')} type="tel" autoComplete="tel" />
            <FieldError>{errors.phone?.message}</FieldError>
          </Field>
          <Field invalid={!!errors.email}>
            <FieldLabel>E-mail</FieldLabel>
            <Input {...register('email')} type="email" autoComplete="email" />
            <FieldError>{errors.email?.message}</FieldError>
          </Field>
        </div>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Endereço e fuso horário</CardTitle>
          <CardDescription>A grade e os relatórios do estabelecimento usam o fuso IANA informado aqui.</CardDescription>
        </CardHeader>
        <div className="grid grid-cols-1 gap-5 md:grid-cols-2 xl:grid-cols-3">
          <Field invalid={!!errors.postal_code}>
            <FieldLabel>CEP</FieldLabel>
            <Input {...register('postal_code')} inputMode="numeric" autoComplete="postal-code" />
            <FieldError>{errors.postal_code?.message}</FieldError>
          </Field>
          <Field invalid={!!errors.address} className="xl:col-span-2">
            <FieldLabel>Logradouro</FieldLabel>
            <Input {...register('address')} autoComplete="address-line1" />
            <FieldError>{errors.address?.message}</FieldError>
          </Field>
          <Field invalid={!!errors.number}>
            <FieldLabel>Número</FieldLabel>
            <Input {...register('number')} />
            <FieldError>{errors.number?.message}</FieldError>
          </Field>
          <Field invalid={!!errors.complement}>
            <FieldLabel>Complemento</FieldLabel>
            <Input {...register('complement')} autoComplete="address-line2" />
            <FieldError>{errors.complement?.message}</FieldError>
          </Field>
          <Field invalid={!!errors.neighborhood}>
            <FieldLabel>Bairro</FieldLabel>
            <Input {...register('neighborhood')} />
            <FieldError>{errors.neighborhood?.message}</FieldError>
          </Field>
          <Field invalid={!!errors.state} required>
            <FieldLabel>UF</FieldLabel>
            <Controller
              control={control}
              name="state"
              render={({ field }) => (
                <OptionSelect
                  value={field.value || null}
                  onChange={(v) => field.onChange(v ?? '')}
                  options={UFS.map((u) => ({ value: u.uf, label: `${u.uf} — ${u.name}` }))}
                  placeholder="Selecione a UF"
                />
              )}
            />
            <FieldError>{errors.state?.message}</FieldError>
          </Field>
          <Field invalid={!!errors.city} required>
            <FieldLabel>Cidade</FieldLabel>
            <Input {...register('city')} autoComplete="address-level2" />
            <FieldError>{errors.city?.message}</FieldError>
          </Field>
          <Field invalid={!!errors.city_ibge_code}>
            <FieldLabel>Código IBGE do município</FieldLabel>
            <Input {...register('city_ibge_code')} inputMode="numeric" />
            <FieldError>{errors.city_ibge_code?.message}</FieldError>
          </Field>
          <Field invalid={!!errors.timezone} className="md:col-span-2 xl:col-span-3">
            <FieldLabel>Fuso horário (IANA)</FieldLabel>
            <div className="flex flex-col gap-2 sm:flex-row">
              <Input {...register('timezone')} placeholder={isEdit ? undefined : 'Deixe em branco para usar a sugestão do servidor'} className="sm:max-w-sm" />
              {suggestion.data && suggestion.data.timezone !== tz ? (
                <Button type="button" variant="secondary" onClick={() => setValue('timezone', suggestion.data!.timezone, { shouldValidate: true })}>
                  <Wand2 aria-hidden="true" />
                  Usar {suggestion.data.timezone}
                </Button>
              ) : null}
            </div>
            <FieldDescription>
              {suggestion.data
                ? `Sugestão do servidor para ${state}${debCity.trim() ? `/${debCity.trim()}` : ''}: ${suggestion.data.timezone} — ${suggestion.data.reason}`
                : 'Escolha a UF para ver a sugestão de fuso (municípios do oeste do AM, Noronha etc. são tratados pelo servidor).'}
            </FieldDescription>
            <FieldError>{errors.timezone?.message}</FieldError>
          </Field>
        </div>
      </Card>

      {!isEdit ? (
        <Card>
          <CardHeader>
            <CardTitle>Localização (coordenadas)</CardTitle>
            <CardDescription>Opcional. Pode ser informada depois no detalhe do estabelecimento.</CardDescription>
          </CardHeader>
          <div className="grid grid-cols-1 gap-5 md:grid-cols-[1fr_1fr_auto] md:items-start">
            <Field invalid={!!errors.lat}>
              <FieldLabel>Latitude</FieldLabel>
              <Input {...register('lat', { onChange: () => setValue('geo_source', 'MANUAL') })} inputMode="decimal" />
              <FieldError>{errors.lat?.message}</FieldError>
            </Field>
            <Field invalid={!!errors.lng}>
              <FieldLabel>Longitude</FieldLabel>
              <Input {...register('lng', { onChange: () => setValue('geo_source', 'MANUAL') })} inputMode="decimal" />
              <FieldError>{errors.lng?.message}</FieldError>
            </Field>
            <div className="flex flex-col gap-1 md:pt-7">
              <Button type="button" variant="secondary" onClick={() => void captureGps()} loading={geo.pending} loadingText="Obtendo localização…">
                <Crosshair aria-hidden="true" />
                Usar GPS deste dispositivo
              </Button>
              <span className="text-xs text-text-muted">O navegador pedirá sua permissão.</span>
            </div>
          </div>
          {geo.error ? (
            <Alert variant="warning">
              <AlertDescription className="text-text">{geo.error}</AlertDescription>
            </Alert>
          ) : null}
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Classificação e comercial</CardTitle>
          <CardDescription>Categorias, segmentos e produtos vendidos são editados no detalhe do estabelecimento.</CardDescription>
        </CardHeader>
        <div className="grid grid-cols-1 gap-5 md:grid-cols-2 xl:grid-cols-3">
          <Field invalid={!!errors.category_id}>
            <FieldLabel>Categoria (ramo)</FieldLabel>
            <Controller
              control={control}
              name="category_id"
              render={({ field }) => (
                <OptionSelect
                  value={field.value}
                  onChange={field.onChange}
                  includeNone
                  noneLabel="Sem categoria"
                  options={treeOrder(nodes.data?.items ?? []).map((n) => ({ value: n.id, label: `${'— '.repeat(n.depth)}${n.name}` }))}
                />
              )}
            />
            <FieldError>{errors.category_id?.message}</FieldError>
          </Field>
          <Field>
            <FieldLabel>Classe do local</FieldLabel>
            <Controller
              control={control}
              name="venue_class"
              render={({ field }) => (
                <OptionSelect
                  value={field.value}
                  onChange={(v) => field.onChange(v ?? 'STANDARD')}
                  options={[
                    { value: 'STANDARD', label: 'Padrão' },
                    { value: 'PREMIUM', label: 'Premium' },
                  ]}
                />
              )}
            />
          </Field>
          <Field invalid={!!errors.weight}>
            <FieldLabel>Peso para comissão</FieldLabel>
            <Input {...register('weight')} inputMode="decimal" />
            <FieldDescription>1 = padrão. Usado no rateio por peso (modelo B).</FieldDescription>
            <FieldError>{errors.weight?.message}</FieldError>
          </Field>
          <Field invalid={!!errors.partner_plan_id}>
            <FieldLabel>Plano do parceiro</FieldLabel>
            <Controller
              control={control}
              name="partner_plan_id"
              render={({ field }) => (
                <OptionSelect
                  value={field.value}
                  onChange={field.onChange}
                  includeNone
                  noneLabel="Sem plano"
                  options={(plans.data?.items ?? []).map((p) => ({ value: p.id, label: p.name }))}
                />
              )}
            />
            <FieldError>{errors.partner_plan_id?.message}</FieldError>
          </Field>
          <Controller
            control={control}
            name="receives_commission"
            render={({ field }) => (
              <Field orientation="horizontal" className="min-h-11 md:pt-7">
                <Switch checked={field.value} onCheckedChange={field.onChange} />
                <FieldLabel>Recebe comissão</FieldLabel>
              </Field>
            )}
          />
          <Controller
            control={control}
            name="pays_monthly_fee"
            render={({ field }) => (
              <Field orientation="horizontal" className="min-h-11 md:pt-7">
                <Switch checked={field.value} onCheckedChange={field.onChange} />
                <FieldLabel>Paga mensalidade</FieldLabel>
              </Field>
            )}
          />
          {paysFee ? (
            <Field invalid={!!errors.monthly_fee} required>
              <FieldLabel>Mensalidade (R$)</FieldLabel>
              <Input {...register('monthly_fee')} inputMode="decimal" placeholder="0,00" />
              <FieldError>{errors.monthly_fee?.message}</FieldError>
            </Field>
          ) : null}
        </div>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Instalação</CardTitle>
        </CardHeader>
        <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
          <Controller
            control={control}
            name="responsible_technician_id"
            render={({ field }) => (
              <TechnicianSelect
                permission="equipment.manage"
                establishmentId={establishment?.id ?? null}
                label="Técnico responsável"
                value={field.value}
                onChange={(id) => field.onChange(id)}
                error={errors.responsible_technician_id?.message}
              />
            )}
          />
          <Field invalid={!!errors.installation_date}>
            <FieldLabel>Data de instalação</FieldLabel>
            <Input {...register('installation_date')} type="date" />
            <FieldError>{errors.installation_date?.message}</FieldError>
          </Field>
          <Field invalid={!!errors.notes} className="md:col-span-2">
            <FieldLabel>Observações</FieldLabel>
            <Textarea {...register('notes')} rows={3} />
            <FieldError>{errors.notes?.message}</FieldError>
          </Field>
          {isEdit ? (
            <Field invalid={!!errors.reason} className="md:col-span-2">
              <FieldLabel>Motivo da alteração</FieldLabel>
              <Textarea {...register('reason')} rows={2} />
              <FieldDescription>Opcional; fica registrado na auditoria (recomendado ao corrigir o fuso).</FieldDescription>
              <FieldError>{errors.reason?.message}</FieldError>
            </Field>
          ) : null}
        </div>
      </Card>

      <div className="flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
        <Button type="button" variant="secondary" onClick={() => navigate(isEdit ? `/estabelecimentos/${establishment!.id}` : '/estabelecimentos')}>
          Cancelar
        </Button>
        <Button type="submit" loading={isSubmitting} loadingText="Salvando…">
          {isEdit ? 'Salvar alterações' : 'Cadastrar estabelecimento'}
        </Button>
      </div>
    </form>
  )
}
