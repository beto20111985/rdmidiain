/** Aba "Dados": fiscais, contato, endereço, coordenadas (PUT /geo), fuso (sugestão + correção), comercial. */
import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Clock, Crosshair, MapPin } from 'lucide-react'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Can } from '@/components/auth/guards'
import { DetailList } from '@/components/ops/detail-list'
import { GeoLink } from '@/components/ops/geo-link'
import { ReasonDialog } from '@/components/ops/reason-dialog'
import { UserName } from '@/components/ops/entity-name'
import { useBrowserGeolocation } from '@/components/ops/use-geolocation'
import { qk } from '@/lib/queryKeys'
import { formatCents, formatDate, formatDateTime, formatNumber } from '@/lib/format'
import { establishmentsApi } from '@/pages/ops/api/client'
import { usePartnerPlans, useTaxonomyNodes } from '@/pages/ops/api/hooks'
import { CLASS_LABEL, REGION_LABEL, labelOf } from '@/pages/ops/api/labels'
import type { Establishment } from '@/pages/ops/api/types'
import { isValidTimeZone } from '../establishmentFormModel'

export function EstablishmentOverview({ est }: { est: Establishment }) {
  const plans = usePartnerPlans(true)
  const nodes = useTaxonomyNodes()
  const plan = plans.data?.items.find((p) => p.id === est.partner_plan_id)
  const category = nodes.data?.items.find((n) => n.id === est.category_id)
  const [tzOpen, setTzOpen] = React.useState(false)
  const [geoOpen, setGeoOpen] = React.useState(false)
  const suggestion = useQuery({
    queryKey: qk.sub('establishments', 'timezone-suggestion', est.state, est.city),
    queryFn: () => establishmentsApi.timezoneSuggestion(est.state, est.city),
    staleTime: Infinity,
    retry: false,
  })

  return (
    <div className="grid grid-cols-1 gap-6 xl:grid-cols-12">
      <Card className="xl:col-span-7">
        <CardHeader>
          <CardTitle>Dados fiscais e contato</CardTitle>
        </CardHeader>
        <DetailList
          items={[
            { label: 'Nome', value: est.name },
            { label: 'Nome fantasia', value: est.trade_name },
            { label: 'Razão social', value: est.legal_name },
            { label: est.document_type ?? 'CPF/CNPJ', value: est.document },
            { label: 'Responsável', value: est.responsible_name },
            { label: 'Telefone', value: est.phone },
            { label: 'E-mail', value: est.email },
            { label: 'Cadastrado em', value: formatDateTime(est.created_at, est.timezone) },
          ]}
        />
      </Card>

      <Card className="xl:col-span-5">
        <CardHeader>
          <CardTitle>Fuso horário</CardTitle>
          <Can permission="establishments.manage">
            <Button variant="secondary" onClick={() => setTzOpen(true)}>
              <Clock aria-hidden="true" />
              Corrigir fuso
            </Button>
          </Can>
        </CardHeader>
        <DetailList
          columns={1}
          items={[
            { label: 'Fuso IANA', value: <span className="font-semibold">{est.timezone}</span> },
            { label: 'Origem', value: est.timezone_source === 'MANUAL' ? 'Corrigido manualmente' : 'Sugerido pelo endereço' },
            {
              label: 'Sugestão do servidor',
              value: suggestion.data ? `${suggestion.data.timezone} — ${suggestion.data.reason}` : suggestion.isLoading ? 'Carregando…' : null,
            },
            { label: 'Hora local agora', value: formatDateTime(new Date(), est.timezone) },
          ]}
        />
        {suggestion.data && suggestion.data.timezone !== est.timezone ? (
          <Alert variant="warning">
            <AlertDescription className="text-text">
              O fuso cadastrado difere da sugestão para {est.city}/{est.state}. Confira antes de publicar campanhas com horário.
            </AlertDescription>
          </Alert>
        ) : null}
      </Card>

      <Card className="xl:col-span-7">
        <CardHeader>
          <CardTitle>Endereço</CardTitle>
        </CardHeader>
        <DetailList
          items={[
            {
              label: 'Logradouro',
              value: [est.address, est.number, est.complement].filter(Boolean).join(', ') || null,
              wide: true,
            },
            { label: 'Bairro', value: est.neighborhood },
            { label: 'CEP', value: est.postal_code },
            { label: 'Cidade/UF', value: `${est.city}/${est.state}` },
            { label: 'Região', value: est.region_label ?? labelOf(REGION_LABEL, est.region) },
            { label: 'Código IBGE', value: est.city_ibge_code },
          ]}
        />
      </Card>

      <Card className="xl:col-span-5">
        <CardHeader>
          <CardTitle>Localização</CardTitle>
          <Can permission="establishments.manage">
            <Button variant="secondary" onClick={() => setGeoOpen(true)}>
              <MapPin aria-hidden="true" />
              Editar coordenadas
            </Button>
          </Can>
        </CardHeader>
        <GeoLink lat={est.lat} lng={est.lng} source={est.geo_source} accuracyM={est.geo_accuracy_m} />
        {est.geo_captured_at ? (
          <p className="m-0 text-xs text-text-muted">Registrada em {formatDateTime(est.geo_captured_at, est.timezone)}</p>
        ) : null}
        <CardDescription>Sem mapa embutido: o painel não carrega mapas de terceiros. Use “Abrir no mapa”.</CardDescription>
      </Card>

      <Card className="xl:col-span-12">
        <CardHeader>
          <CardTitle>Classificação, comercial e instalação</CardTitle>
        </CardHeader>
        <DetailList
          columns={3}
          items={[
            { label: 'Categoria (ramo)', value: category?.name ?? (est.category_id ? '—' : 'Sem categoria') },
            { label: 'Classe do local', value: labelOf(CLASS_LABEL, est.venue_class) },
            { label: 'Peso para comissão', value: formatNumber((est.weight_milli ?? 1000) / 1000, { maximumFractionDigits: 3 }), numeric: true },
            { label: 'Plano do parceiro', value: plan?.name ?? (est.partner_plan_id ? '—' : 'Sem plano') },
            { label: 'Recebe comissão', value: est.receives_commission ? 'Sim' : 'Não' },
            { label: 'Mensalidade', value: est.pays_monthly_fee ? formatCents(est.monthly_fee_cents) : 'Não paga' },
            { label: 'Técnico responsável', value: <UserName id={est.responsible_technician_id} /> },
            { label: 'Data de instalação', value: est.installation_date ? formatDate(`${est.installation_date}T12:00:00Z`, est.timezone) : null },
            { label: 'Observações', value: est.notes, wide: true },
          ]}
        />
      </Card>

      <TimezoneDialog est={est} open={tzOpen} onOpenChange={setTzOpen} suggested={suggestion.data?.timezone ?? null} />
      <GeoDialog est={est} open={geoOpen} onOpenChange={setGeoOpen} />
    </div>
  )
}

function TimezoneDialog({
  est,
  open,
  onOpenChange,
  suggested,
}: {
  est: Establishment
  open: boolean
  onOpenChange: (o: boolean) => void
  suggested: string | null
}) {
  const qc = useQueryClient()
  const [tz, setTz] = React.useState(est.timezone)
  React.useEffect(() => {
    if (open) setTz(est.timezone)
  }, [open, est.timezone])
  const valid = isValidTimeZone(tz.trim()) && tz.trim() !== est.timezone
  const m = useMutation({
    mutationFn: (reason: string) => establishmentsApi.update(est.id, { timezone: tz.trim(), reason }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['establishments'] })
      toast.success('Fuso corrigido. A programação das telas será regenerada.')
    },
  })
  return (
    <ReasonDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Corrigir fuso horário"
      description="O fuso define a hora local da grade, dos horários específicos e da validade das campanhas nesta unidade."
      consequences={['A programação de todas as telas do estabelecimento é regenerada no novo fuso.']}
      confirmLabel="Salvar fuso"
      extraValid={valid}
      onConfirm={(reason) => m.mutateAsync(reason)}
    >
      <Field invalid={!!tz.trim() && !isValidTimeZone(tz.trim())} required>
        <FieldLabel>Fuso IANA</FieldLabel>
        <Input value={tz} onChange={(e) => setTz(e.target.value)} spellCheck={false} />
        <FieldDescription>
          Ex.: America/Sao_Paulo, America/Manaus, America/Rio_Branco, America/Noronha.
          {suggested ? ` Sugestão para o endereço: ${suggested}.` : ''}
        </FieldDescription>
        <FieldError>{tz.trim() && !isValidTimeZone(tz.trim()) ? 'Fuso IANA inválido.' : null}</FieldError>
      </Field>
      {suggested && suggested !== tz ? (
        <Button type="button" variant="tertiary" className="w-fit" onClick={() => setTz(suggested)}>
          Usar a sugestão {suggested}
        </Button>
      ) : null}
    </ReasonDialog>
  )
}

function GeoDialog({ est, open, onOpenChange }: { est: Establishment; open: boolean; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient()
  const geo = useBrowserGeolocation()
  const [lat, setLat] = React.useState('')
  const [lng, setLng] = React.useState('')
  const [source, setSource] = React.useState<'MANUAL' | 'GPS'>('MANUAL')
  const [acc, setAcc] = React.useState<number | null>(null)
  React.useEffect(() => {
    if (open) {
      setLat(est.lat !== null ? String(est.lat) : '')
      setLng(est.lng !== null ? String(est.lng) : '')
      setSource('MANUAL')
      setAcc(null)
    }
  }, [open, est.lat, est.lng])
  const nLat = Number(lat.replace(',', '.'))
  const nLng = Number(lng.replace(',', '.'))
  const latOk = lat.trim() !== '' && Number.isFinite(nLat) && nLat >= -90 && nLat <= 90
  const lngOk = lng.trim() !== '' && Number.isFinite(nLng) && nLng >= -180 && nLng <= 180
  const m = useMutation({
    mutationFn: () => establishmentsApi.setGeo(est.id, { lat: nLat, lng: nLng, source, accuracy_m: acc }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['establishments'] })
      toast.success('Coordenadas salvas.')
    },
  })
  return (
    <ReasonDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Editar coordenadas"
      description="Informe manualmente ou capture pelo GPS deste dispositivo (estando no local)."
      confirmLabel="Salvar coordenadas"
      minReason={0}
      reasonLabel="Observação (opcional)"
      reasonHint="A alteração é registrada na auditoria."
      extraValid={latOk && lngOk}
      onConfirm={() => m.mutateAsync()}
    >
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Field invalid={lat.trim() !== '' && !latOk} required>
          <FieldLabel>Latitude</FieldLabel>
          <Input value={lat} onChange={(e) => { setLat(e.target.value); setSource('MANUAL') }} inputMode="decimal" />
          <FieldError>{lat.trim() !== '' && !latOk ? 'Entre -90 e 90.' : null}</FieldError>
        </Field>
        <Field invalid={lng.trim() !== '' && !lngOk} required>
          <FieldLabel>Longitude</FieldLabel>
          <Input value={lng} onChange={(e) => { setLng(e.target.value); setSource('MANUAL') }} inputMode="decimal" />
          <FieldError>{lng.trim() !== '' && !lngOk ? 'Entre -180 e 180.' : null}</FieldError>
        </Field>
      </div>
      <div className="flex flex-col gap-1">
        <Button
          type="button"
          variant="secondary"
          className="w-fit"
          loading={geo.pending}
          loadingText="Obtendo localização…"
          onClick={async () => {
            const r = await geo.request()
            if (!r) return
            setLat(String(r.lat))
            setLng(String(r.lng))
            setSource('GPS')
            setAcc(r.accuracy_m)
          }}
        >
          <Crosshair aria-hidden="true" />
          Usar GPS deste dispositivo
        </Button>
        <span className="text-xs text-text-muted">
          {source === 'GPS' ? `Capturada por GPS${acc !== null ? ` (precisão ≈ ${acc} m)` : ''}.` : 'O navegador pedirá sua permissão.'}
        </span>
        {geo.error ? <span className="text-xs text-danger">{geo.error}</span> : null}
      </div>
    </ReasonDialog>
  )
}
