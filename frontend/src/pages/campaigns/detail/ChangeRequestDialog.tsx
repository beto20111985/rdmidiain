/**
 * Alteração pós-aprovação (REQ-CMP-006/011): região/datas/telas/duração/anúncios geram uma
 * NOVA REVISÃO com preço recalculado pelo servidor e nova aprovação; a versão vigente continua
 * tocando até a aprovação. Diferença positiva gera cobrança (Idempotency-Key por tentativa).
 */
import * as React from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Calculator } from 'lucide-react'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { ScheduleEditor, type ScheduleValue } from '@/components/campaigns/schedule-editor'
import { TargetingEditor } from '@/components/campaigns/targeting-editor'
import { qk } from '@/lib/queryKeys'
import { newRequestId } from '@/lib/http'
import { isApiError } from '@/lib/errors'
import { formatCents } from '@/lib/format'
import { campaignsApi, creativesApi, pricingApi, taxonomyApi, type ChangeResponse } from '../lib/api'
import { ALLOWED_DURATIONS } from '../lib/rules'
import { scheduleBody, scheduleFrom, validateSchedule } from '../lib/schedule'
import type { useCampaignArea } from '../lib/hooks'
import { CheckoutError } from '../checkout/shared'
import type { CampaignDetail, Target } from '../lib/types'
import { describeError, isPaymentFlowError } from '@/pages/campaigns/lib/errorText'

export function ChangeRequestDialog({
  campaign,
  open,
  onOpenChange,
  area,
}: {
  campaign: CampaignDetail
  open: boolean
  onOpenChange: (o: boolean) => void
  area: ReturnType<typeof useCampaignArea>
}) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [withSchedule, setWithSchedule] = React.useState(false)
  const [withTargets, setWithTargets] = React.useState(false)
  const [withCreatives, setWithCreatives] = React.useState(false)
  const [schedule, setSchedule] = React.useState<ScheduleValue>(() => scheduleFrom(campaign))
  const [targets, setTargets] = React.useState<Target[]>(campaign.targets)
  const [duration, setDuration] = React.useState(campaign.duration_s)
  const [replacements, setReplacements] = React.useState<Record<string, string>>({})
  const [errors, setErrors] = React.useState<Record<string, string>>({})
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<unknown>(null)
  const [result, setResult] = React.useState<ChangeResponse | null>(null)
  const [estimate, setEstimate] = React.useState<{ total: number } | null>(null)
  const [estimating, setEstimating] = React.useState(false)
  const keyRef = React.useRef<string | null>(null)

  // Reinicia o formulário SÓ ao abrir: a campanha é recarregada (WS/invalidação) logo após o envio,
  // e reiniciar a cada nova referência apagaria o resultado (e o botão "Pagar diferença").
  const wasOpen = React.useRef(false)
  React.useEffect(() => {
    const opening = open && !wasOpen.current
    wasOpen.current = open
    if (opening) {
      setSchedule(scheduleFrom(campaign))
      setTargets(campaign.targets)
      setDuration(campaign.duration_s)
      setReplacements({})
      setWithSchedule(false)
      setWithTargets(false)
      setWithCreatives(false)
      setErrors({})
      setError(null)
      setResult(null)
      setEstimate(null)
      keyRef.current = null
    }
  }, [open, campaign])

  const nodes = useQuery({ queryKey: qk.sub('taxonomy', 'nodes'), queryFn: () => taxonomyApi.nodes(), enabled: open && withTargets, staleTime: 600000 })
  const creatives = useQuery({
    queryKey: qk.list('creatives', { org_id: campaign.org_id, status: 'RASCUNHO,APROVADO', page_size: 100 }),
    queryFn: () => creativesApi.list({ org_id: campaign.org_id, status: 'RASCUNHO,APROVADO', page_size: 100 }),
    enabled: open && withCreatives,
  })
  const active = campaign.creatives.filter((c) => c.is_active && !c.pending_removal)
  const options = (creatives.data?.items ?? []).filter((c) => c.duration_seconds === duration && !active.some((a) => a.creative_id === c.id))

  const buildBody = (): Record<string, unknown> | null => {
    const e: Record<string, string> = {}
    const body: Record<string, unknown> = {}
    if (withSchedule) {
      Object.assign(e, validateSchedule(schedule))
      Object.assign(body, scheduleBody(schedule))
    }
    if (withTargets) {
      if (!targets.some((t) => !t.exclude)) e.targets = 'Inclua ao menos um local.'
      body.targets = targets
    }
    if (withCreatives) {
      const reps = Object.entries(replacements).filter(([, n]) => n)
      if (duration !== campaign.duration_s) {
        body.duration_s = duration
        if (reps.length < active.length) e.creatives = 'Ao mudar a duração, substitua todos os anúncios por anúncios da nova duração.'
      }
      if (reps.length) body.creative_replacements = reps.map(([old_creative_id, new_creative_id]) => ({ old_creative_id, new_creative_id }))
      if (!reps.length && duration === campaign.duration_s) e.creatives = 'Escolha ao menos um anúncio para substituir.'
    }
    if (!withSchedule && !withTargets && !withCreatives) e.general = 'Marque o que deseja alterar.'
    setErrors(e)
    return Object.keys(e).length ? null : body
  }

  const simulate = async () => {
    const body = buildBody()
    if (!body) return
    setEstimating(true)
    setError(null)
    try {
      const s = withSchedule ? schedule : scheduleFrom(campaign)
      // campaign_id: telas e preço pela mesma elegibilidade do checkout (conflitos aplicados) = new_price_cents
      const r = await pricingApi.quotePreview({
        campaign_id: campaign.id,
        targets: withTargets ? targets : campaign.targets,
        duration_s: withCreatives ? duration : campaign.duration_s,
        freq_kind: s.freq_kind,
        freq_value: s.freq_kind === 'SPECIFIC_TIME' ? s.specific_times.length : s.freq_value,
        specific_times: s.freq_kind === 'SPECIFIC_TIME' ? s.specific_times : null,
        days_mask: s.days_mask,
        time_bands: s.time_bands,
        start_date: s.start_date,
        period_type: s.period_type,
        ...(s.period_type === 'AVULSO' ? { end_date: s.end_date } : {}),
        priority: s.priority,
        exclusivity: s.exclusivity,
        plan_id: campaign.plan_id,
      })
      setEstimate({ total: r.quote.total_cents })
    } catch (e) {
      setError(e)
    } finally {
      setEstimating(false)
    }
  }

  const submit = async () => {
    const body = buildBody()
    if (!body) return
    setPending(true)
    setError(null)
    if (!keyRef.current) keyRef.current = newRequestId()
    try {
      const r = await campaignsApi.requestChange(campaign.id, body, keyRef.current)
      keyRef.current = null
      setResult(r)
      toast.success(`Alteração enviada (revisão ${r.revision}).`)
      void qc.invalidateQueries({ queryKey: qk.root('campaigns') })
    } catch (e) {
      // Falha de rede: mantém a mesma chave (repetição idempotente); demais erros: nova tentativa = nova chave.
      if (!(isApiError(e) && e.isNetwork)) keyRef.current = null
      setError(e)
    } finally {
      setPending(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => (!pending ? onOpenChange(o) : undefined)}>
      <DialogContent size="md" className="max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Solicitar alteração</DialogTitle>
          <DialogDescription>
            A alteração vira uma nova versão com preço recalculado pelo servidor e passa por nova análise. A versão atual continua no ar até a aprovação.
          </DialogDescription>
        </DialogHeader>
        {result ? (
          <div className="flex flex-col gap-3" role="status">
            <Alert variant="success">
              <AlertTitle>Revisão {result.revision} criada</AlertTitle>
              <AlertDescription>
                Preço anterior {formatCents(result.previous_price_cents)} · novo preço {formatCents(result.new_price_cents)} · diferença{' '}
                {formatCents(result.diff_cents)}
                {result.credit_due_cents > 0 ? ` · crédito a seu favor ${formatCents(result.credit_due_cents)} (tratado pelo financeiro)` : ''}.
              </AlertDescription>
            </Alert>
            {result.order ? (
              <p className="m-0 text-sm">
                A diferença precisa ser paga antes da análise. Cobrança gerada: {formatCents(result.order.total_cents)}.
              </p>
            ) : (
              <p className="m-0 text-sm">Sem valor a pagar: a alteração foi para análise da RD.</p>
            )}
            <DialogFooter>
              {result.order && !area.isStaff ? (
                <Button onClick={() => navigate(area.checkoutPath(result.order!.order_id))}>Pagar diferença</Button>
              ) : null}
              <Button variant="secondary" onClick={() => onOpenChange(false)}>
                Fechar
              </Button>
            </DialogFooter>
          </div>
        ) : (
          <div className="flex flex-col gap-5">
            <Section title="Período e horários" checked={withSchedule} onChecked={setWithSchedule}>
              <ScheduleEditor value={schedule} onChange={(p) => setSchedule((v) => ({ ...v, ...p }))} errors={errors} />
            </Section>
            <Section title="Locais (segmentação)" checked={withTargets} onChecked={setWithTargets}>
              <TargetingEditor targets={targets} onChange={setTargets} isStaff={area.isStaff} categories={nodes.data?.items ?? []} error={errors.targets} />
            </Section>
            <Section title="Duração e anúncios" checked={withCreatives} onChecked={setWithCreatives}>
              <div className="flex flex-col gap-4">
                <div className="flex flex-col gap-2">
                  <span id="change-duration" className="text-sm font-semibold">
                    Duração
                  </span>
                  <ToggleGroup type="single" value={String(duration)} onValueChange={(v) => v && setDuration(Number(v))} aria-labelledby="change-duration">
                    {ALLOWED_DURATIONS.map((d) => (
                      <ToggleGroupItem key={d} value={String(d)}>
                        {d} s
                      </ToggleGroupItem>
                    ))}
                  </ToggleGroup>
                </div>
                {active.map((a) => (
                  <Field key={a.creative_id}>
                    <FieldLabel>Substituir {a.code ?? 'anúncio'} ({a.name ?? '—'}) por</FieldLabel>
                    <Select value={replacements[a.creative_id] ?? '__keep'} onValueChange={(v) => setReplacements((r) => ({ ...r, [a.creative_id]: v === '__keep' ? '' : v }))}>
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectGroup>
                          <SelectItem value="__keep">Manter</SelectItem>
                          {options.map((c) => (
                            <SelectItem key={c.id} value={c.id}>
                              {c.code} · {c.name}
                            </SelectItem>
                          ))}
                        </SelectGroup>
                      </SelectContent>
                    </Select>
                    <FieldDescription>Somente anúncios de {duration} s. Crie novos anúncios na campanha ou na biblioteca antes.</FieldDescription>
                  </Field>
                ))}
                {errors.creatives ? <p className="m-0 text-sm text-danger">{errors.creatives}</p> : null}
              </div>
            </Section>
            {errors.general ? <p role="alert" className="m-0 text-sm text-danger">{errors.general}</p> : null}
            {estimate ? (
              <Alert variant="info">
                <Calculator aria-hidden="true" />
                <AlertDescription>
                  Estimativa do servidor para a campanha alterada: {formatCents(estimate.total)} (valor contratado hoje:{' '}
                  {formatCents(campaign.contracted_price_cents)}). A diferença definitiva é calculada ao enviar.
                </AlertDescription>
              </Alert>
            ) : null}
            {error ? (
              isPaymentFlowError(error) ? (
                <CheckoutError error={error} />
              ) : (
                <Alert variant="danger">
                  <AlertDescription>{describeError(error)}</AlertDescription>
                </Alert>
              )
            ) : null}
            <DialogFooter>
              <Button variant="secondary" onClick={() => onOpenChange(false)} disabled={pending}>
                Cancelar
              </Button>
              <Button variant="secondary" onClick={() => void simulate()} loading={estimating} disabled={pending}>
                <Calculator aria-hidden="true" />
                Simular preço
              </Button>
              <Button onClick={() => void submit()} loading={pending}>
                Enviar alteração
              </Button>
            </DialogFooter>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

function Section({ title, checked, onChecked, children }: { title: string; checked: boolean; onChecked: (v: boolean) => void; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-3 rounded-lg border border-border p-3">
      <Field orientation="horizontal" className="min-h-11">
        <Checkbox checked={checked} onCheckedChange={(c) => onChecked(c === true)} />
        <FieldLabel>Alterar {title.toLowerCase()}</FieldLabel>
      </Field>
      {checked ? children : null}
    </section>
  )
}
