/**
 * Planos do parceiro (RD): mensalidade opcional (gratuito = R$ 0), TV própria × TV da RD em comodato,
 * caução, instalação, política de upgrade, comissão e rotação padrão sugerida (interno + externo).
 * GET/POST/PATCH /partner-plans (criar/editar: establishments.manage global).
 */
import * as React from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Layers, Pencil, Plus } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Can } from '@/components/auth/guards'
import { Card, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Switch } from '@/components/ui/switch'
import { Skeleton } from '@/components/ui/skeleton'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { EmptyState, ErrorState, NoPermission } from '@/components/patterns/states'
import { DetailList } from '@/components/ops/detail-list'
import { OptionSelect } from '@/components/ops/filter-select'
import { centsToInput, parseBrlToCents } from '@/components/ops/utils'
import { useCan } from '@/hooks/usePermissions'
import { errorMessage, isApiError } from '@/lib/errors'
import { formatCents } from '@/lib/format'
import { plansApi } from '@/pages/ops/api/client'
import { usePartnerPlans } from '@/pages/ops/api/hooks'
import { TV_PROVISION_LABEL, UPGRADE_POLICY_LABEL, labelOf } from '@/pages/ops/api/labels'
import type { PartnerPlan } from '@/pages/ops/api/types'
import { rotationValid } from './planRules'

export default function PartnerPlansPage() {
  const can = useCan()
  const [showInactive, setShowInactive] = React.useState(false)
  const q = usePartnerPlans(showInactive)
  const [editing, setEditing] = React.useState<PartnerPlan | 'new' | null>(null)
  if (isApiError(q.error) && q.error.isForbidden) return <NoPermission permission="establishments.view" />
  const items = q.data?.items ?? []
  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Planos do parceiro"
        description="Condições comerciais oferecidas aos estabelecimentos. A cobrança (mensalidade, caução, instalação) é feita pelo Financeiro."
        actions={
          <Can permission="establishments.manage">
            <Button onClick={() => setEditing('new')}>
              <Plus aria-hidden="true" />
              Novo plano
            </Button>
          </Can>
        }
      />
      <Field orientation="horizontal" className="min-h-11 w-auto">
        <Switch checked={showInactive} onCheckedChange={setShowInactive} />
        <FieldLabel>Mostrar planos inativos</FieldLabel>
      </Field>
      {q.isLoading ? (
        <Skeleton className="h-48 w-full rounded-xl" />
      ) : q.error ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} />
      ) : !items.length ? (
        <EmptyState
          icon={Layers}
          title="Nenhum plano cadastrado."
          description="Crie planos (ex.: gratuito com TV própria; mensal com TV da RD em comodato) para padronizar as condições."
          action={
            can('establishments.manage') ? (
              <Button onClick={() => setEditing('new')}>Novo plano</Button>
            ) : undefined
          }
        />
      ) : (
        <div className="grid grid-cols-1 gap-6 md:grid-cols-2 xl:grid-cols-3">
          {items.map((p) => (
            <Card key={p.id}>
              <CardHeader>
                <CardTitle>{p.name}</CardTitle>
                <div className="flex gap-2">
                  {!p.is_active ? <Badge tone="neutral">Inativo</Badge> : null}
                  {p.is_free ? <Badge tone="success">Gratuito</Badge> : null}
                </div>
              </CardHeader>
              {p.description ? <p className="m-0 text-sm text-text-muted">{p.description}</p> : null}
              <DetailList
                columns={1}
                items={[
                  { label: 'Mensalidade', value: p.monthly_fee_cents ? `${formatCents(p.monthly_fee_cents)}/mês` : 'Sem mensalidade' },
                  { label: 'TV', value: labelOf(TV_PROVISION_LABEL, p.tv_provision) + (p.default_tv_inches ? ` · ${p.default_tv_inches}"` : '') },
                  { label: 'Caução', value: p.deposit_required ? formatCents(p.deposit_cents) : 'Não exige' },
                  { label: 'Instalação', value: p.installation_fee_required ? formatCents(p.installation_fee_cents) : 'Gratuita' },
                  {
                    label: 'Upgrade de TV',
                    value: labelOf(UPGRADE_POLICY_LABEL, p.upgrade_policy) + (p.upgrade_policy === 'PAID' ? ` (${formatCents(p.upgrade_fee_cents)})` : ''),
                  },
                  { label: 'Comissão', value: p.receives_commission ? 'Recebe' : 'Não recebe' },
                  {
                    label: 'Rotação padrão sugerida',
                    value:
                      p.default_internal_s != null && p.default_external_s != null
                        ? `${p.default_internal_s / 60} min interno + ${p.default_external_s / 60} min externo`
                        : 'Padrão da rede',
                  },
                ]}
              />
              <Can permission="establishments.manage">
                <Button variant="secondary" className="w-fit" onClick={() => setEditing(p)}>
                  <Pencil aria-hidden="true" />
                  Editar
                </Button>
              </Can>
            </Card>
          ))}
        </div>
      )}
      <PlanDialog plan={editing} onClose={() => setEditing(null)} />
    </div>
  )
}

function PlanDialog({ plan, onClose }: { plan: PartnerPlan | 'new' | null; onClose: () => void }) {
  const qc = useQueryClient()
  const isNew = plan === 'new'
  const p = plan && plan !== 'new' ? plan : null
  const [v, setV] = React.useState(() => init(p))
  const [touched, setTouched] = React.useState(false)
  React.useEffect(() => {
    if (plan) {
      setV(init(p))
      setTouched(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plan])
  const set = <K extends keyof ReturnType<typeof init>>(k: K, val: ReturnType<typeof init>[K]) => setV((s) => ({ ...s, [k]: val }))

  const fee = parseBrlToCents(v.fee) ?? 0
  const dep = v.depositRequired ? parseBrlToCents(v.deposit) : 0
  const inst = v.installRequired ? parseBrlToCents(v.install) : 0
  const up = v.upgrade === 'PAID' ? parseBrlToCents(v.upgradeFee) : null
  const iS = v.internalMin.trim() === '' ? null : Math.round(Number(v.internalMin.replace(',', '.')) * 60)
  const eS = v.externalMin.trim() === '' ? null : Math.round(Number(v.externalMin.replace(',', '.')) * 60)
  const errs = {
    name: v.name.trim().length < 2 ? 'Informe o nome do plano.' : null,
    fee: Number.isNaN(fee) ? 'Valor inválido (ex.: 99,90).' : null,
    deposit: v.depositRequired && (dep === null || Number.isNaN(dep)) ? 'Informe a caução.' : null,
    install: v.installRequired && (inst === null || Number.isNaN(inst)) ? 'Informe a taxa de instalação.' : null,
    upgrade: v.upgrade === 'PAID' && (up === null || Number.isNaN(up)) ? 'Informe o valor do upgrade.' : null,
    rotation:
      (iS === null) !== (eS === null)
        ? 'Informe tempo interno e externo juntos (ou nenhum).'
        : iS !== null && eS !== null && !rotationValid(iS, eS)
          ? 'Tempos em múltiplos de 30 s, externo ≥ 30 s e ciclo (interno + externo) divisor de 60 min (ex.: 2 + 2, 3 + 1, 1 + 1).'
          : null,
  }
  const valid = !Object.values(errs).some(Boolean)

  const m = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = {
        name: v.name.trim(),
        description: v.description.trim() || null,
        monthly_fee_cents: fee || 0,
        default_tv_inches: v.inches.trim() ? Number(v.inches) : null,
        deposit_required: v.depositRequired,
        deposit_cents: v.depositRequired ? dep : 0,
        installation_fee_required: v.installRequired,
        installation_fee_cents: v.installRequired ? inst : 0,
        upgrade_policy: v.upgrade,
        upgrade_fee_cents: up,
        receives_commission: v.commission,
        default_internal_s: iS,
        default_external_s: eS,
      }
      if (isNew) return plansApi.create({ ...body, tv_provision: v.tv })
      return plansApi.update(p!.id, { ...body, is_active: v.active })
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['establishments'] })
      toast.success(isNew ? 'Plano criado.' : 'Plano atualizado.')
      onClose()
    },
  })

  return (
    <Dialog open={!!plan} onOpenChange={(o) => !o && !m.isPending && onClose()}>
      <DialogContent size="md">
        <form
          noValidate
          className="flex flex-col gap-5"
          onSubmit={(e) => {
            e.preventDefault()
            setTouched(true)
            if (valid) m.mutate()
          }}
        >
          <DialogHeader>
            <DialogTitle>{isNew ? 'Novo plano do parceiro' : `Editar ${p?.name}`}</DialogTitle>
            <DialogDescription>Alterações valem para novos vínculos; acordos de comodato existentes guardam seus próprios termos.</DialogDescription>
          </DialogHeader>
          <Field invalid={touched && !!errs.name} required>
            <FieldLabel>Nome</FieldLabel>
            <Input value={v.name} onChange={(e) => set('name', e.target.value)} maxLength={120} />
            <FieldError>{touched ? errs.name : null}</FieldError>
          </Field>
          <Field>
            <FieldLabel>Descrição</FieldLabel>
            <Textarea value={v.description} onChange={(e) => set('description', e.target.value)} rows={2} maxLength={4000} />
          </Field>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <Field invalid={touched && !!errs.fee}>
              <FieldLabel>Mensalidade (R$)</FieldLabel>
              <Input value={v.fee} onChange={(e) => set('fee', e.target.value)} inputMode="decimal" placeholder="0,00" />
              <FieldDescription>0 = plano gratuito.</FieldDescription>
              <FieldError>{touched ? errs.fee : null}</FieldError>
            </Field>
            <Field>
              <FieldLabel>TV</FieldLabel>
              <OptionSelect
                value={v.tv}
                onChange={(x) => x && set('tv', x as typeof v.tv)}
                disabled={!isNew}
                options={Object.entries(TV_PROVISION_LABEL).map(([value, label]) => ({ value, label }))}
              />
              {!isNew ? <FieldDescription>Não muda após a criação.</FieldDescription> : null}
            </Field>
            <Field>
              <FieldLabel>Polegadas padrão</FieldLabel>
              <Input value={v.inches} onChange={(e) => set('inches', e.target.value.replace(/\D/g, ''))} inputMode="numeric" />
            </Field>
            <Field>
              <FieldLabel>Upgrade de TV</FieldLabel>
              <OptionSelect
                value={v.upgrade}
                onChange={(x) => x && set('upgrade', x as typeof v.upgrade)}
                options={Object.entries(UPGRADE_POLICY_LABEL).map(([value, label]) => ({ value, label }))}
              />
            </Field>
            {v.upgrade === 'PAID' ? (
              <Field invalid={touched && !!errs.upgrade} required>
                <FieldLabel>Valor do upgrade (R$)</FieldLabel>
                <Input value={v.upgradeFee} onChange={(e) => set('upgradeFee', e.target.value)} inputMode="decimal" />
                <FieldError>{touched ? errs.upgrade : null}</FieldError>
              </Field>
            ) : null}
            <Field orientation="horizontal" className="min-h-11">
              <Switch checked={v.depositRequired} onCheckedChange={(c) => set('depositRequired', c)} />
              <FieldLabel>Exige caução</FieldLabel>
            </Field>
            {v.depositRequired ? (
              <Field invalid={touched && !!errs.deposit} required>
                <FieldLabel>Caução (R$)</FieldLabel>
                <Input value={v.deposit} onChange={(e) => set('deposit', e.target.value)} inputMode="decimal" />
                <FieldError>{touched ? errs.deposit : null}</FieldError>
              </Field>
            ) : (
              <span />
            )}
            <Field orientation="horizontal" className="min-h-11">
              <Switch checked={v.installRequired} onCheckedChange={(c) => set('installRequired', c)} />
              <FieldLabel>Instalação paga</FieldLabel>
            </Field>
            {v.installRequired ? (
              <Field invalid={touched && !!errs.install} required>
                <FieldLabel>Taxa de instalação (R$)</FieldLabel>
                <Input value={v.install} onChange={(e) => set('install', e.target.value)} inputMode="decimal" />
                <FieldError>{touched ? errs.install : null}</FieldError>
              </Field>
            ) : (
              <span />
            )}
            <Field orientation="horizontal" className="min-h-11">
              <Switch checked={v.commission} onCheckedChange={(c) => set('commission', c)} />
              <FieldLabel>Recebe comissão</FieldLabel>
            </Field>
            {!isNew ? (
              <Field orientation="horizontal" className="min-h-11">
                <Switch checked={v.active} onCheckedChange={(c) => set('active', c)} />
                <FieldLabel>Plano ativo</FieldLabel>
              </Field>
            ) : null}
          </div>
          <fieldset className="m-0 flex flex-col gap-3 border-0 p-0">
            <legend className="mb-1 text-sm font-semibold text-text">Rotação padrão sugerida (opcional)</legend>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <Field invalid={touched && !!errs.rotation}>
                <FieldLabel>Interno (minutos)</FieldLabel>
                <Input value={v.internalMin} onChange={(e) => set('internalMin', e.target.value)} inputMode="decimal" placeholder="2" />
              </Field>
              <Field invalid={touched && !!errs.rotation}>
                <FieldLabel>Externo (minutos)</FieldLabel>
                <Input value={v.externalMin} onChange={(e) => set('externalMin', e.target.value)} inputMode="decimal" placeholder="2" />
              </Field>
            </div>
            {touched && errs.rotation ? <p className="m-0 text-xs text-danger">{errs.rotation}</p> : null}
            <p className="m-0 text-xs text-text-muted">Sem valores, vale a política da rede (padrão 2 min + 2 min).</p>
          </fieldset>
          {m.error ? (
            <Alert variant="danger">
              <AlertDescription className="text-text">{errorMessage(m.error)}</AlertDescription>
            </Alert>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={onClose} disabled={m.isPending}>
              Cancelar
            </Button>
            <Button type="submit" loading={m.isPending}>
              {isNew ? 'Criar plano' : 'Salvar plano'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function init(p: PartnerPlan | null) {
  return {
    name: p?.name ?? '',
    description: p?.description ?? '',
    fee: centsToInput(p?.monthly_fee_cents ?? 0),
    tv: (p?.tv_provision ?? 'OWN_TV') as 'OWN_TV' | 'RD_TV_COMODATO',
    inches: p?.default_tv_inches ? String(p.default_tv_inches) : '',
    depositRequired: p?.deposit_required ?? false,
    deposit: centsToInput(p?.deposit_cents ?? 0),
    installRequired: p?.installation_fee_required ?? false,
    install: centsToInput(p?.installation_fee_cents ?? 0),
    upgrade: (p?.upgrade_policy ?? 'NEGOTIATED') as 'FREE' | 'PAID' | 'NEGOTIATED',
    upgradeFee: centsToInput(p?.upgrade_fee_cents ?? null),
    commission: p?.receives_commission ?? true,
    internalMin: p?.default_internal_s != null ? String(p.default_internal_s / 60).replace('.', ',') : '',
    externalMin: p?.default_external_s != null ? String(p.default_external_s / 60).replace('.', ',') : '',
    active: p?.is_active ?? true,
  }
}
