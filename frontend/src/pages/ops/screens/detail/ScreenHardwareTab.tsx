/**
 * Hardware da tela: TV e player cadastrados, equipamentos instalados (Estoque) e TROCA de TV/TV Box
 * com histórico (POST /screens/{id}/replace-hardware): item novo do estoque (opcional), destino do
 * item antigo (estoque/manutenção) e motivo.
 */
import * as React from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Replace } from 'lucide-react'
import { Card, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { DetailList } from '@/components/ops/detail-list'
import { OptionSelect } from '@/components/ops/filter-select'
import { ReasonDialog } from '@/components/ops/reason-dialog'
import { useCan } from '@/hooks/usePermissions'
import { qk } from '@/lib/queryKeys'
import { formatDateTime } from '@/lib/format'
import { equipmentApi, screensApi } from '@/pages/ops/api/client'
import {
  CLASS_LABEL,
  DISPLAY_TYPE_LABEL,
  EQUIPMENT_STATUS_LABEL,
  EQUIPMENT_TYPE_LABEL,
  ORIENTATION_LABEL,
  PLAYER_TYPE_LABEL,
  PROGRAMMING_MODE_LABEL,
  TV_OWNER_LABEL,
  labelOf,
} from '@/pages/ops/api/labels'
import type { Screen } from '@/pages/ops/api/types'
import { UserName } from '@/components/ops/entity-name'

export function ScreenHardwareTab({ screen }: { screen: Screen }) {
  const can = useCan()
  const tz = screen.establishment?.timezone
  const h = screen.hardware
  const [open, setOpen] = React.useState(false)
  return (
    <div className="grid grid-cols-1 gap-6 xl:grid-cols-12">
      <Card className="xl:col-span-6">
        <CardHeader>
          <CardTitle>TV / painel</CardTitle>
          {can('screens.manage') && screen.status !== 'DECOMMISSIONED' ? (
            <Button variant="secondary" onClick={() => setOpen(true)}>
              <Replace aria-hidden="true" />
              Trocar TV ou TV Box
            </Button>
          ) : null}
        </CardHeader>
        <DetailList
          items={[
            { label: 'Tipo', value: labelOf(DISPLAY_TYPE_LABEL, screen.display_type) },
            { label: 'Propriedade', value: labelOf(TV_OWNER_LABEL, screen.tv_owner) },
            { label: 'Marca/modelo', value: [h.brand, h.model].filter(Boolean).join(' ') || null },
            { label: 'Polegadas', value: h.inches, numeric: true },
            { label: 'Resolução', value: h.resolution_w && h.resolution_h ? `${h.resolution_w} × ${h.resolution_h}` : null },
            { label: 'Orientação', value: labelOf(ORIENTATION_LABEL, screen.orientation) },
            { label: 'Nº de série', value: h.serial_number },
            { label: 'Patrimônio', value: h.asset_tag },
            { label: 'Instalada em', value: formatDateTime(h.installed_at, tz) },
            { label: 'Instalada por', value: <UserName id={h.installed_by} /> },
          ]}
        />
      </Card>
      <Card className="xl:col-span-6">
        <CardHeader>
          <CardTitle>Player</CardTitle>
        </CardHeader>
        <DetailList
          items={[
            { label: 'Tipo de player', value: labelOf(PLAYER_TYPE_LABEL, screen.player_type) },
            { label: 'Marca/modelo', value: [h.player_brand, h.player_model].filter(Boolean).join(' ') || null },
            { label: 'Nº de série', value: h.player_serial_number },
            { label: 'Classe', value: labelOf(CLASS_LABEL, screen.screen_class) },
            { label: 'Programação', value: labelOf(PROGRAMMING_MODE_LABEL, screen.programming_mode) },
          ]}
        />
      </Card>
      <Card className="xl:col-span-12">
        <CardHeader>
          <CardTitle>Equipamentos instalados (estoque)</CardTitle>
        </CardHeader>
        {screen.equipment === null || screen.equipment === undefined ? (
          <p className="m-0 text-sm text-text-muted">Seu perfil não consulta o estoque (equipment.view).</p>
        ) : !screen.equipment.length ? (
          <p className="m-0 text-sm text-text-muted">Nenhum item do estoque vinculado a esta tela.</p>
        ) : (
          <ul className="m-0 flex list-none flex-col divide-y divide-border p-0">
            {screen.equipment.map((it) => (
              <li key={it.id} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
                <Link to={`/estoque/${it.id}`} className="font-semibold text-link underline-offset-4 hover:underline">
                  {labelOf(EQUIPMENT_TYPE_LABEL, it.type)} · {it.name}
                </Link>
                <span className="text-text-muted">
                  {it.serial_number ? `Série ${it.serial_number}` : ''}
                  {it.asset_tag ? ` · Patrimônio ${it.asset_tag}` : ''} · {labelOf(EQUIPMENT_STATUS_LABEL, it.status)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>
      <ReplaceDialog screen={screen} open={open} onOpenChange={setOpen} />
    </div>
  )
}

function ReplaceDialog({ screen, open, onOpenChange }: { screen: Screen; open: boolean; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient()
  const can = useCan()
  const canStock = can('equipment.manage')
  const [component, setComponent] = React.useState<'TV' | 'PLAYER'>('TV')
  const [itemId, setItemId] = React.useState<string | null>(null)
  const [dest, setDest] = React.useState<string | null>('WAREHOUSE')
  const [brand, setBrand] = React.useState('')
  const [model, setModel] = React.useState('')
  const [serial, setSerial] = React.useState('')
  React.useEffect(() => {
    if (open) {
      setComponent('TV')
      setItemId(null)
      setDest('WAREHOUSE')
      setBrand('')
      setModel('')
      setSerial('')
    }
  }, [open])
  const stock = useQuery({
    queryKey: qk.list('equipment', { type: component === 'TV' ? 'TV' : 'TVBOX', status: 'IN_STOCK', page_size: 50 }),
    queryFn: () => equipmentApi.list({ type: component === 'TV' ? 'TV' : 'TVBOX', status: 'IN_STOCK', page_size: 50 }),
    enabled: open && canStock,
  })
  const m = useMutation({
    mutationFn: (reason: string) => {
      const hardware: Record<string, string> = {}
      if (!itemId) {
        if (component === 'TV') {
          if (brand.trim()) hardware.hardware_brand = brand.trim()
          if (model.trim()) hardware.hardware_model = model.trim()
          if (serial.trim()) hardware.serial_number = serial.trim()
        } else {
          if (brand.trim()) hardware.player_brand = brand.trim()
          if (model.trim()) hardware.player_model = model.trim()
          if (serial.trim()) hardware.player_serial_number = serial.trim()
        }
      }
      return screensApi.replaceHardware(screen.id, {
        component,
        new_equipment_item_id: itemId,
        old_item_destination: canStock && (screen.equipment ?? []).length ? dest : null,
        hardware,
        reason,
      })
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['screens'] })
      void qc.invalidateQueries({ queryKey: ['equipment'] })
      toast.success('Troca registrada no histórico da tela.')
    },
  })
  return (
    <ReasonDialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Trocar hardware da tela ${screen.friendly_id}`}
      description="O código da tela, o histórico e as exibições são preservados."
      confirmLabel="Registrar troca"
      extraValid={!!itemId || !!(brand.trim() || model.trim() || serial.trim())}
      onConfirm={(reason) => m.mutateAsync(reason)}
    >
      <div className="flex flex-col gap-2">
        <span id="replace-comp" className="text-sm font-semibold text-text">
          Componente
        </span>
        <ToggleGroup type="single" value={component} onValueChange={(v) => {
            if (!v) return
            setComponent(v as 'TV' | 'PLAYER')
            setItemId(null)
          }} aria-labelledby="replace-comp">
          <ToggleGroupItem value="TV">TV</ToggleGroupItem>
          <ToggleGroupItem value="PLAYER">TV Box / player</ToggleGroupItem>
        </ToggleGroup>
      </div>
      {canStock ? (
        <Field>
          <FieldLabel>Item novo do estoque</FieldLabel>
          <OptionSelect
            value={itemId}
            onChange={setItemId}
            includeNone
            noneLabel="Não vincular item do estoque"
            options={(stock.data?.items ?? []).map((i) => ({
              value: i.id,
              label: `${i.name}${i.serial_number ? ` · série ${i.serial_number}` : ''}${i.asset_tag ? ` · ${i.asset_tag}` : ''}`,
            }))}
          />
          <FieldDescription>Itens “Em estoque” do tipo escolhido. A movimentação é registrada no estoque.</FieldDescription>
        </Field>
      ) : null}
      {canStock && (screen.equipment ?? []).length ? (
        <Field>
          <FieldLabel>Destino do item retirado</FieldLabel>
          <OptionSelect
            value={dest}
            onChange={setDest}
            options={[
              { value: 'WAREHOUSE', label: 'Volta ao estoque' },
              { value: 'MAINTENANCE', label: 'Vai para manutenção' },
            ]}
          />
        </Field>
      ) : null}
      {!itemId ? (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field>
            <FieldLabel>Marca nova</FieldLabel>
            <Input value={brand} onChange={(e) => setBrand(e.target.value)} />
          </Field>
          <Field>
            <FieldLabel>Modelo novo</FieldLabel>
            <Input value={model} onChange={(e) => setModel(e.target.value)} />
          </Field>
          <Field>
            <FieldLabel>Nº de série novo</FieldLabel>
            <Input value={serial} onChange={(e) => setSerial(e.target.value)} />
          </Field>
        </div>
      ) : null}
    </ReasonDialog>
  )
}
