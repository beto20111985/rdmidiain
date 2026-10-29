/**
 * Novo acordo de comodato (TV da RD no estabelecimento) — POST /comodato-agreements.
 * Caução, instalação e upgrade configuráveis (valores em centavos); termos podem vir do plano.
 * A cobrança das taxas é do Financeiro (W-FIN): aqui ficam só os termos acordados.
 */
import * as React from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Switch } from '@/components/ui/switch'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { OptionSelect } from '@/components/ops/filter-select'
import { EstablishmentPicker, ScreenPicker } from '@/components/ops/pickers'
import { centsToInput, parseBrlToCents } from '@/components/ops/utils'
import { errorMessage } from '@/lib/errors'
import { comodatoApi } from '@/pages/ops/api/client'
import { usePartnerPlans } from '@/pages/ops/api/hooks'
import { PAID_BY_LABEL, UPGRADE_POLICY_LABEL } from '@/pages/ops/api/labels'
import type { ComodatoAgreement } from '@/pages/ops/api/types'

export function ComodatoFormDialog({
  open,
  onOpenChange,
  establishmentId,
  onCreated,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  /** Fixo quando aberto a partir do detalhe do estabelecimento. */
  establishmentId?: string
  onCreated?: (a: ComodatoAgreement) => void
}) {
  const qc = useQueryClient()
  const plans = usePartnerPlans(false)
  const [estId, setEstId] = React.useState<string | null>(establishmentId ?? null)
  const [screenId, setScreenId] = React.useState<string | null>(null)
  const [planId, setPlanId] = React.useState<string | null>(null)
  const [depositReq, setDepositReq] = React.useState(false)
  const [deposit, setDeposit] = React.useState('')
  const [instReq, setInstReq] = React.useState(false)
  const [inst, setInst] = React.useState('')
  const [upgrade, setUpgrade] = React.useState<string>('NEGOTIATED')
  const [upgradeFee, setUpgradeFee] = React.useState('')
  const [maint, setMaint] = React.useState('RD')
  const [breakage, setBreakage] = React.useState('ESTABLISHMENT')
  const [notes, setNotes] = React.useState('')
  const [touched, setTouched] = React.useState(false)

  React.useEffect(() => {
    if (open) {
      setEstId(establishmentId ?? null)
      setScreenId(null)
      setPlanId(null)
      setDepositReq(false)
      setDeposit('')
      setInstReq(false)
      setInst('')
      setUpgrade('NEGOTIATED')
      setUpgradeFee('')
      setMaint('RD')
      setBreakage('ESTABLISHMENT')
      setNotes('')
      setTouched(false)
    }
  }, [open, establishmentId])

  const applyPlan = (id: string | null) => {
    setPlanId(id)
    const p = plans.data?.items.find((x) => x.id === id)
    if (!p) return
    setDepositReq(p.deposit_required)
    setDeposit(centsToInput(p.deposit_cents))
    setInstReq(p.installation_fee_required)
    setInst(centsToInput(p.installation_fee_cents))
    setUpgrade(p.upgrade_policy)
    setUpgradeFee(centsToInput(p.upgrade_fee_cents))
  }

  const depCents = depositReq ? parseBrlToCents(deposit) : 0
  const instCents = instReq ? parseBrlToCents(inst) : 0
  const upCents = upgrade === 'PAID' ? parseBrlToCents(upgradeFee) : null
  const errs = {
    est: !estId ? 'Selecione o estabelecimento.' : null,
    deposit: depositReq && (depCents === null || Number.isNaN(depCents)) ? 'Informe o valor da caução.' : null,
    inst: instReq && (instCents === null || Number.isNaN(instCents)) ? 'Informe a taxa de instalação.' : null,
    upgrade: upgrade === 'PAID' && (upCents === null || Number.isNaN(upCents)) ? 'Informe o valor do upgrade.' : null,
  }
  const valid = !Object.values(errs).some(Boolean)

  const m = useMutation({
    mutationFn: () =>
      comodatoApi.create({
        establishment_id: estId,
        screen_id: screenId,
        partner_plan_id: planId,
        deposit_required: depositReq,
        deposit_cents: depositReq ? depCents : 0,
        installation_fee_required: instReq,
        installation_fee_cents: instReq ? instCents : 0,
        upgrade_policy: upgrade,
        upgrade_fee_cents: upCents,
        maintenance_paid_by: maint,
        breakage_paid_by: breakage,
        notes: notes.trim() || null,
      }),
    onSuccess: (a) => {
      void qc.invalidateQueries({ queryKey: ['establishments'] })
      toast.success('Acordo de comodato registrado.')
      onCreated?.(a)
      onOpenChange(false)
    },
  })

  return (
    <Dialog open={open} onOpenChange={(o) => (!m.isPending ? onOpenChange(o) : undefined)}>
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
            <DialogTitle>Novo acordo de comodato</DialogTitle>
            <DialogDescription>TV (e acessórios) da RD cedidos ao estabelecimento. Os itens são vinculados no Estoque.</DialogDescription>
          </DialogHeader>
          {!establishmentId ? (
            <EstablishmentPicker value={estId} onChange={setEstId} required onlyActive error={touched ? errs.est : null} />
          ) : null}
          {estId ? (
            <ScreenPicker
              value={screenId}
              onChange={setScreenId}
              establishmentId={estId}
              legend="Tela (opcional)"
              description="Vincule à tela quando o comodato for de uma TV específica."
            />
          ) : null}
          <Field>
            <FieldLabel>Plano do parceiro (preenche os termos)</FieldLabel>
            <OptionSelect
              value={planId}
              onChange={applyPlan}
              includeNone
              noneLabel="Sem plano"
              options={(plans.data?.items ?? []).map((p) => ({ value: p.id, label: p.name }))}
            />
          </Field>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <Field orientation="horizontal" className="min-h-11">
              <Switch checked={depositReq} onCheckedChange={setDepositReq} />
              <FieldLabel>Exige caução</FieldLabel>
            </Field>
            {depositReq ? (
              <Field invalid={touched && !!errs.deposit} required>
                <FieldLabel>Caução (R$)</FieldLabel>
                <Input value={deposit} onChange={(e) => setDeposit(e.target.value)} inputMode="decimal" />
                <FieldError>{touched ? errs.deposit : null}</FieldError>
              </Field>
            ) : (
              <span />
            )}
            <Field orientation="horizontal" className="min-h-11">
              <Switch checked={instReq} onCheckedChange={setInstReq} />
              <FieldLabel>Instalação paga</FieldLabel>
            </Field>
            {instReq ? (
              <Field invalid={touched && !!errs.inst} required>
                <FieldLabel>Taxa de instalação (R$)</FieldLabel>
                <Input value={inst} onChange={(e) => setInst(e.target.value)} inputMode="decimal" />
                <FieldError>{touched ? errs.inst : null}</FieldError>
              </Field>
            ) : (
              <span />
            )}
            <Field>
              <FieldLabel>Upgrade de TV</FieldLabel>
              <OptionSelect
                value={upgrade}
                onChange={(v) => setUpgrade(v ?? 'NEGOTIATED')}
                options={Object.entries(UPGRADE_POLICY_LABEL).map(([value, label]) => ({ value, label }))}
              />
            </Field>
            {upgrade === 'PAID' ? (
              <Field invalid={touched && !!errs.upgrade} required>
                <FieldLabel>Valor do upgrade (R$)</FieldLabel>
                <Input value={upgradeFee} onChange={(e) => setUpgradeFee(e.target.value)} inputMode="decimal" />
                <FieldError>{touched ? errs.upgrade : null}</FieldError>
              </Field>
            ) : (
              <span />
            )}
            <Field>
              <FieldLabel>Manutenção paga por</FieldLabel>
              <OptionSelect value={maint} onChange={(v) => setMaint(v ?? 'RD')} options={Object.entries(PAID_BY_LABEL).map(([value, label]) => ({ value, label }))} />
            </Field>
            <Field>
              <FieldLabel>Quebra/dano pago por</FieldLabel>
              <OptionSelect
                value={breakage}
                onChange={(v) => setBreakage(v ?? 'ESTABLISHMENT')}
                options={Object.entries(PAID_BY_LABEL).map(([value, label]) => ({ value, label }))}
              />
            </Field>
          </div>
          <Field>
            <FieldLabel>Observações</FieldLabel>
            <Textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={2} maxLength={4000} />
            <FieldDescription>O contrato de comodato é aceito pelo responsável do estabelecimento.</FieldDescription>
          </Field>
          {m.error ? (
            <Alert variant="danger">
              <AlertDescription className="text-text">{errorMessage(m.error)}</AlertDescription>
            </Alert>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)} disabled={m.isPending}>
              Cancelar
            </Button>
            <Button type="submit" loading={m.isPending}>
              Registrar comodato
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
