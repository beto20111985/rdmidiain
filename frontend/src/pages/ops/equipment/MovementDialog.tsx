/**
 * Nova movimentação de estoque (append-only) — POST /equipment/{id}/movements.
 * Tipos e locais válidos seguem a matriz do backend (entrada, transferência, instalação, empréstimo,
 * comodato, devolução, manutenção, RMA, baixa, ajuste). Itens por quantidade exigem a origem (saldo).
 */
import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { OptionSelect } from '@/components/ops/filter-select'
import { EstablishmentPicker, ScreenPicker, TechnicianSelect } from '@/components/ops/pickers'
import { CityKeyFields } from '@/components/ops/location-label'
import { errorMessage } from '@/lib/errors'
import { qk } from '@/lib/queryKeys'
import { comodatoApi, equipmentApi } from '@/pages/ops/api/client'
import { LOCATION_KIND_LABEL, MOVEMENT_RULES, MOVEMENT_TYPE_LABEL, labelOf } from '@/pages/ops/api/labels'
import type { EquipmentItem, ItemBalance, LocationKind, MovementType } from '@/pages/ops/api/types'
import { allowedMovements } from './movementRules'

const REASON_REQUIRED: MovementType[] = ['WRITE_OFF', 'ADJUSTMENT', 'RMA_OUT']

function balanceKey(b: ItemBalance) {
  return `${b.kind}|${b.ref_id ?? ''}|${b.city_key ?? ''}`
}

export function MovementDialog({ item, open, onOpenChange }: { item: EquipmentItem; open: boolean; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient()
  const types = allowedMovements(item)
  const [type, setType] = React.useState<MovementType | null>(null)
  const [toKind, setToKind] = React.useState<LocationKind | null>(null)
  const [refId, setRefId] = React.useState<string | null>(null)
  const [refEst, setRefEst] = React.useState<string | null>(null)
  const [uf, setUf] = React.useState<string | null>(null)
  const [city, setCity] = React.useState('')
  const [source, setSource] = React.useState<string | null>(null)
  const [qty, setQty] = React.useState('1')
  const [reason, setReason] = React.useState('')
  const [reference, setReference] = React.useState('')
  const [returnAt, setReturnAt] = React.useState('')
  const [agreement, setAgreement] = React.useState<string | null>(null)
  const [touched, setTouched] = React.useState(false)

  React.useEffect(() => {
    if (open) {
      setType(null)
      setToKind(null)
      setRefId(null)
      setRefEst(null)
      setUf(null)
      setCity('')
      setSource(null)
      setQty('1')
      setReason('')
      setReference('')
      setReturnAt('')
      setAgreement(null)
      setTouched(false)
    }
  }, [open])

  const rule = type ? MOVEMENT_RULES[type] : null
  const bulk = item.tracking === 'BULK'
  const sources: Array<{ value: string; label: string; b: ItemBalance | null }> = !rule
    ? []
    : [
        ...(rule.from.includes('EXTERNAL') ? [{ value: 'EXTERNAL||', label: 'Externo (fornecedor/ajuste)', b: null }] : []),
        ...(item.balances ?? [])
          .filter((b) => rule.from.includes(b.kind) && b.quantity > 0)
          .map((b) => ({
            value: balanceKey(b),
            label: `${labelOf(LOCATION_KIND_LABEL, b.kind)}${b.city_key ? ` · ${b.city_key}` : ''} — saldo ${b.quantity}`,
            b,
          })),
      ]
  const destKinds = rule ? rule.to : []
  const estForAgreement = toKind === 'ESTABLISHMENT' ? refId : toKind === 'SCREEN' ? refEst : null
  const agreements = useQuery({
    queryKey: qk.sub('establishments', 'comodato', 'by-establishment', estForAgreement ?? ''),
    queryFn: () => comodatoApi.list({ establishment_id: estForAgreement!, status: 'ACTIVE', page_size: 20 }),
    enabled: type === 'COMODATO' && !!estForAgreement,
  })

  const n = Number(qty)
  const errs = {
    type: !type ? 'Escolha o tipo de movimentação.' : null,
    to: !toKind ? 'Escolha o destino.' : null,
    ref:
      toKind && ['SCREEN', 'ESTABLISHMENT', 'TECHNICIAN'].includes(toKind) && !refId
        ? 'Selecione o destino.'
        : (toKind === 'WAREHOUSE' || toKind === 'MAINTENANCE') && uf && city.trim().length < 2
          ? 'Informe a cidade.'
          : null,
    source: bulk && !source ? 'Escolha a origem (saldo).' : null,
    qty: bulk && (!Number.isInteger(n) || n < 1) ? 'Quantidade inteira maior que zero.' : null,
    reason: type && REASON_REQUIRED.includes(type) && reason.trim().length < 3 ? 'Justificativa obrigatória.' : null,
    reference: type === 'RMA_OUT' && !reference.trim() ? 'Informe o protocolo do RMA.' : null,
    returnAt: type === 'LOAN' && !returnAt ? 'Informe a data prevista de devolução.' : null,
    agreement: type === 'COMODATO' && !agreement ? 'Selecione o acordo de comodato.' : null,
  }
  const valid = !Object.values(errs).some(Boolean)

  const m = useMutation({
    mutationFn: () => {
      const to: Record<string, unknown> = { kind: toKind }
      if (toKind && ['SCREEN', 'ESTABLISHMENT', 'TECHNICIAN'].includes(toKind)) to.ref_id = refId
      if ((toKind === 'WAREHOUSE' || toKind === 'MAINTENANCE') && uf && city.trim()) to.city_key = `${uf}:${city.trim()}`
      const body: Record<string, unknown> = {
        movement_type: type,
        to,
        quantity: bulk ? n : 1,
        reason: reason.trim() || null,
        reference: reference.trim() || null,
        expected_return_at: returnAt || null,
        comodato_agreement_id: type === 'COMODATO' ? agreement : null,
      }
      if (bulk && source) {
        const s = sources.find((x) => x.value === source)
        body.from = s?.b ? { kind: s.b.kind, ref_id: s.b.ref_id, city_key: s.b.city_key } : { kind: 'EXTERNAL' }
      }
      return equipmentApi.move(item.id, body)
    },
    onSuccess: (r) => {
      void qc.invalidateQueries({ queryKey: ['equipment'] })
      void qc.invalidateQueries({ queryKey: ['screens'] })
      toast.success(`Movimentação registrada: ${labelOf(MOVEMENT_TYPE_LABEL, r.movement.movement_type)}.`)
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
            <DialogTitle>Nova movimentação — {item.name}</DialogTitle>
            <DialogDescription>Movimentações não podem ser editadas nem apagadas; para corrigir, registre uma nova.</DialogDescription>
          </DialogHeader>
          {!types.length ? (
            <Alert variant="warning">
              <AlertDescription className="text-text">Este item foi baixado e não pode mais ser movimentado.</AlertDescription>
            </Alert>
          ) : (
            <>
              <Field invalid={touched && !!errs.type} required>
                <FieldLabel>Tipo de movimentação</FieldLabel>
                <OptionSelect
                  value={type}
                  onChange={(v) => {
                    setType(v as MovementType | null)
                    const r = v ? MOVEMENT_RULES[v as MovementType] : null
                    setToKind(r && r.to.length === 1 ? r.to[0] : null)
                    setRefId(null)
                    setSource(null)
                  }}
                  options={types.map((t) => ({ value: t, label: MOVEMENT_TYPE_LABEL[t] }))}
                />
                <FieldError>{touched ? errs.type : null}</FieldError>
              </Field>
              {bulk && rule ? (
                <Field invalid={touched && !!errs.source} required>
                  <FieldLabel>Origem</FieldLabel>
                  <OptionSelect value={source} onChange={setSource} options={sources.map(({ value, label }) => ({ value, label }))} placeholder={sources.length ? 'Selecione' : 'Sem saldo em origem válida'} />
                  <FieldError>{touched ? errs.source : null}</FieldError>
                </Field>
              ) : null}
              {rule ? (
                <Field invalid={touched && !!errs.to} required>
                  <FieldLabel>Destino</FieldLabel>
                  <OptionSelect
                    value={toKind}
                    onChange={(v) => {
                      setToKind(v as LocationKind | null)
                      setRefId(null)
                    }}
                    options={destKinds.map((k) => ({ value: k, label: LOCATION_KIND_LABEL[k] }))}
                  />
                  <FieldError>{touched ? errs.to : null}</FieldError>
                </Field>
              ) : null}
              {toKind === 'SCREEN' ? (
                <ScreenPicker
                  value={refId}
                  onChange={(id, s) => {
                    setRefId(id)
                    setRefEst(s?.establishment_id ?? null)
                  }}
                  required
                  error={touched ? errs.ref : null}
                />
              ) : toKind === 'ESTABLISHMENT' ? (
                <EstablishmentPicker value={refId} onChange={setRefId} required onlyActive error={touched ? errs.ref : null} />
              ) : toKind === 'TECHNICIAN' ? (
                <TechnicianSelect permission="equipment.manage" value={refId} onChange={setRefId} required error={touched ? errs.ref : null} />
              ) : toKind === 'WAREHOUSE' || toKind === 'MAINTENANCE' ? (
                <div className="flex flex-col gap-1">
                  <CityKeyFields legend={toKind === 'WAREHOUSE' ? 'Estoque (cidade)' : 'Manutenção (cidade)'} uf={uf} city={city} onChange={(u, c) => { setUf(u); setCity(c) }} />
                  {touched && errs.ref ? <p className="m-0 text-xs text-danger">{errs.ref}</p> : null}
                </div>
              ) : null}
              {type === 'COMODATO' ? (
                <Field invalid={touched && !!errs.agreement} required>
                  <FieldLabel>Acordo de comodato</FieldLabel>
                  <OptionSelect
                    value={agreement}
                    onChange={setAgreement}
                    placeholder={!estForAgreement ? 'Escolha o destino primeiro' : 'Selecione'}
                    options={(agreements.data?.items ?? []).map((a) => ({ value: a.id, label: `Acordo ativo desde ${a.started_at?.slice(0, 10) ?? '—'}` }))}
                  />
                  <FieldDescription>Registre o acordo no estabelecimento antes de enviar o item.</FieldDescription>
                  <FieldError>{touched ? errs.agreement : null}</FieldError>
                </Field>
              ) : null}
              {bulk ? (
                <Field invalid={touched && !!errs.qty} required>
                  <FieldLabel>Quantidade</FieldLabel>
                  <Input value={qty} onChange={(e) => setQty(e.target.value.replace(/\D/g, ''))} inputMode="numeric" />
                  <FieldError>{touched ? errs.qty : null}</FieldError>
                </Field>
              ) : null}
              {type === 'LOAN' ? (
                <Field invalid={touched && !!errs.returnAt} required>
                  <FieldLabel>Devolução prevista</FieldLabel>
                  <Input type="date" value={returnAt} onChange={(e) => setReturnAt(e.target.value)} />
                  <FieldError>{touched ? errs.returnAt : null}</FieldError>
                </Field>
              ) : null}
              <Field invalid={touched && !!errs.reference} required={type === 'RMA_OUT'}>
                <FieldLabel>{type === 'RMA_OUT' ? 'Protocolo do RMA' : 'Referência (opcional)'}</FieldLabel>
                <Input value={reference} onChange={(e) => setReference(e.target.value)} maxLength={120} />
                <FieldError>{touched ? errs.reference : null}</FieldError>
              </Field>
              <Field invalid={touched && !!errs.reason} required={!!type && REASON_REQUIRED.includes(type)}>
                <FieldLabel>Justificativa</FieldLabel>
                <Textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={2} maxLength={2000} />
                <FieldError>{touched ? errs.reason : null}</FieldError>
              </Field>
            </>
          )}
          {m.error ? (
            <Alert variant="danger">
              <AlertDescription className="text-text">{errorMessage(m.error)}</AlertDescription>
            </Alert>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)} disabled={m.isPending}>
              Cancelar
            </Button>
            <Button type="submit" loading={m.isPending} disabled={!types.length}>
              Registrar movimentação
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
