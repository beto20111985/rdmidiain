/**
 * Entrada no estoque — POST /equipment: item com nº de série (1 unidade, série/patrimônio únicos)
 * ou por quantidade (cabos, suportes…). A entrada gera a 1ª movimentação (ENTRY) do ledger.
 */
import * as React from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { OptionSelect } from '@/components/ops/filter-select'
import { CityKeyFields } from '@/components/ops/location-label'
import { emptyToNull, parseBrlToCents } from '@/components/ops/utils'
import { isApiError, errorMessage } from '@/lib/errors'
import { equipmentApi } from '@/pages/ops/api/client'
import { CONDITION_LABEL, EQUIPMENT_TYPE_LABEL } from '@/pages/ops/api/labels'

const CRUMBS = [{ label: 'Estoque', to: '/estoque' }, { label: 'Entrada no estoque' }]

export default function EquipmentCreatePage() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [v, setV] = React.useState({
    type: 'TV',
    tracking: 'SERIAL' as 'SERIAL' | 'BULK',
    name: '',
    quantity: '1',
    unit: 'UN',
    uf: null as string | null,
    city: '',
    brand: '',
    model: '',
    serial: '',
    asset: '',
    inches: '',
    condition: 'NEW',
    supplier: '',
    purchaseDate: '',
    cost: '',
    warranty: '',
    reference: '',
    notes: '',
  })
  const [touched, setTouched] = React.useState(false)
  const set = <K extends keyof typeof v>(k: K, val: (typeof v)[K]) => setV((s) => ({ ...s, [k]: val }))
  const qty = Number(v.quantity)
  const cost = parseBrlToCents(v.cost)
  const errs = {
    name: v.name.trim().length < 2 ? 'Informe o nome do item (ex.: TV 43" Samsung).' : null,
    quantity: v.tracking === 'BULK' && (!Number.isInteger(qty) || qty < 0 || qty > 100000) ? 'Quantidade entre 0 e 100000.' : null,
    cost: cost !== null && Number.isNaN(cost) ? 'Valor inválido (ex.: 1.299,90).' : null,
    inches: v.inches && (Number(v.inches) < 1 || Number(v.inches) > 500) ? 'Polegadas entre 1 e 500.' : null,
    city: v.uf && v.city.trim().length < 2 ? 'Informe a cidade do estoque.' : null,
  }
  const valid = !Object.values(errs).some(Boolean)

  const m = useMutation({
    mutationFn: () =>
      equipmentApi.create({
        type: v.type,
        tracking: v.tracking,
        name: v.name.trim(),
        quantity: v.tracking === 'BULK' ? qty : 1,
        unit: v.unit.trim() || 'UN',
        warehouse_city_key: v.uf && v.city.trim() ? `${v.uf}:${v.city.trim()}` : null,
        brand: emptyToNull(v.brand),
        model: emptyToNull(v.model),
        serial_number: v.tracking === 'SERIAL' ? emptyToNull(v.serial) : null,
        asset_tag: v.tracking === 'SERIAL' ? emptyToNull(v.asset) : null,
        inches: v.inches ? Number(v.inches) : null,
        condition: v.condition,
        supplier: emptyToNull(v.supplier),
        purchase_date: emptyToNull(v.purchaseDate),
        purchase_cost_cents: cost,
        warranty_until: emptyToNull(v.warranty),
        reference: emptyToNull(v.reference),
        notes: emptyToNull(v.notes),
      }),
    onSuccess: (it) => {
      void qc.invalidateQueries({ queryKey: ['equipment'] })
      toast.success('Entrada registrada no estoque.')
      navigate(`/estoque/${it.id}`)
    },
  })

  const dupField = isApiError(m.error) && m.error.code === 'EQUIPMENT_DUPLICATE' ? (m.error.details.field as string | undefined) : undefined

  return (
    <div className="flex flex-col gap-6">
      <PageHeader title="Entrada no estoque" breadcrumbs={CRUMBS} />
      <form
        noValidate
        className="flex flex-col gap-6"
        onSubmit={(e) => {
          e.preventDefault()
          setTouched(true)
          if (valid) m.mutate()
        }}
      >
        <Card>
          <CardHeader>
            <CardTitle>Item</CardTitle>
          </CardHeader>
          <div className="grid grid-cols-1 gap-5 md:grid-cols-2 xl:grid-cols-3">
            <Field required>
              <FieldLabel>Tipo</FieldLabel>
              <OptionSelect value={v.type} onChange={(x) => x && set('type', x)} options={Object.entries(EQUIPMENT_TYPE_LABEL).map(([value, label]) => ({ value, label }))} />
            </Field>
            <div className="flex flex-col gap-2">
              <span id="eq-tracking" className="text-sm font-semibold text-text">
                Controle
              </span>
              <ToggleGroup type="single" value={v.tracking} onValueChange={(x) => x && set('tracking', x as 'SERIAL' | 'BULK')} aria-labelledby="eq-tracking">
                <ToggleGroupItem value="SERIAL">Nº de série (unidade)</ToggleGroupItem>
                <ToggleGroupItem value="BULK">Por quantidade</ToggleGroupItem>
              </ToggleGroup>
            </div>
            <Field invalid={touched && !!errs.name} required>
              <FieldLabel>Nome</FieldLabel>
              <Input value={v.name} onChange={(e) => set('name', e.target.value)} maxLength={160} />
              <FieldError>{touched ? errs.name : null}</FieldError>
            </Field>
            {v.tracking === 'BULK' ? (
              <>
                <Field invalid={touched && !!errs.quantity} required>
                  <FieldLabel>Quantidade de entrada</FieldLabel>
                  <Input value={v.quantity} onChange={(e) => set('quantity', e.target.value.replace(/\D/g, ''))} inputMode="numeric" />
                  <FieldError>{touched ? errs.quantity : null}</FieldError>
                </Field>
                <Field>
                  <FieldLabel>Unidade</FieldLabel>
                  <Input value={v.unit} onChange={(e) => set('unit', e.target.value.slice(0, 10))} />
                </Field>
              </>
            ) : (
              <>
                <Field invalid={dupField === 'serial_number'}>
                  <FieldLabel>Nº de série</FieldLabel>
                  <Input value={v.serial} onChange={(e) => set('serial', e.target.value)} autoComplete="off" />
                  <FieldError>{dupField === 'serial_number' ? 'Já existe item deste tipo com este nº de série.' : null}</FieldError>
                </Field>
                <Field invalid={dupField === 'asset_tag'}>
                  <FieldLabel>Patrimônio</FieldLabel>
                  <Input value={v.asset} onChange={(e) => set('asset', e.target.value)} autoComplete="off" />
                  <FieldError>{dupField === 'asset_tag' ? 'Patrimônio já cadastrado.' : null}</FieldError>
                </Field>
              </>
            )}
            <Field>
              <FieldLabel>Marca</FieldLabel>
              <Input value={v.brand} onChange={(e) => set('brand', e.target.value)} />
            </Field>
            <Field>
              <FieldLabel>Modelo</FieldLabel>
              <Input value={v.model} onChange={(e) => set('model', e.target.value)} />
            </Field>
            {v.type === 'TV' ? (
              <Field invalid={touched && !!errs.inches}>
                <FieldLabel>Polegadas</FieldLabel>
                <Input value={v.inches} onChange={(e) => set('inches', e.target.value.replace(/\D/g, ''))} inputMode="numeric" />
                <FieldError>{touched ? errs.inches : null}</FieldError>
              </Field>
            ) : null}
            <Field>
              <FieldLabel>Estado de conservação</FieldLabel>
              <OptionSelect value={v.condition} onChange={(x) => x && set('condition', x)} options={Object.entries(CONDITION_LABEL).map(([value, label]) => ({ value, label }))} />
            </Field>
          </div>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Compra e destino</CardTitle>
          </CardHeader>
          <div className="grid grid-cols-1 gap-5 md:grid-cols-2 xl:grid-cols-3">
            <div className="md:col-span-2 xl:col-span-3">
              <CityKeyFields legend="Estoque de destino (cidade)" uf={v.uf} city={v.city} onChange={(uf, city) => setV((s) => ({ ...s, uf, city }))} />
              <FieldDescription>Sem cidade: estoque central (somente perfis com acesso global).</FieldDescription>
              {touched && errs.city ? <p className="m-0 text-xs text-danger">{errs.city}</p> : null}
            </div>
            <Field>
              <FieldLabel>Fornecedor</FieldLabel>
              <Input value={v.supplier} onChange={(e) => set('supplier', e.target.value)} />
            </Field>
            <Field>
              <FieldLabel>Data da compra</FieldLabel>
              <Input type="date" value={v.purchaseDate} onChange={(e) => set('purchaseDate', e.target.value)} />
            </Field>
            <Field invalid={touched && !!errs.cost}>
              <FieldLabel>Custo de compra (R$)</FieldLabel>
              <Input value={v.cost} onChange={(e) => set('cost', e.target.value)} inputMode="decimal" />
              <FieldError>{touched ? errs.cost : null}</FieldError>
            </Field>
            <Field>
              <FieldLabel>Garantia até</FieldLabel>
              <Input type="date" value={v.warranty} onChange={(e) => set('warranty', e.target.value)} />
            </Field>
            <Field>
              <FieldLabel>Referência (nota fiscal/pedido)</FieldLabel>
              <Input value={v.reference} onChange={(e) => set('reference', e.target.value)} maxLength={120} />
            </Field>
            <Field className="md:col-span-2 xl:col-span-3">
              <FieldLabel>Observações</FieldLabel>
              <Textarea value={v.notes} onChange={(e) => set('notes', e.target.value)} rows={2} maxLength={4000} />
            </Field>
          </div>
        </Card>
        {m.error && !dupField ? (
          <Alert variant="danger">
            <AlertDescription className="text-text">{errorMessage(m.error)}</AlertDescription>
          </Alert>
        ) : null}
        <div className="flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
          <Button type="button" variant="secondary" onClick={() => navigate('/estoque')}>
            Cancelar
          </Button>
          <Button type="submit" loading={m.isPending}>
            Registrar entrada
          </Button>
        </div>
      </form>
    </div>
  )
}
