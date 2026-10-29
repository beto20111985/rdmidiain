/** Local de um item do estoque em texto (tipo + estabelecimento/tela/técnico/cidade). */
import { EstablishmentName, ScreenCode, UserName } from './entity-name'
import { LOCATION_KIND_LABEL, labelOf } from '@/pages/ops/api/labels'
import { Field, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { OptionSelect } from './filter-select'
import { UFS } from '@/pages/ops/api/labels'

function cityText(key: string | null | undefined): string | null {
  if (!key) return null
  const [uf, slug] = key.split(':')
  return slug ? `${slug.replace(/-/g, ' ')}/${uf}` : key
}

export function LocationLabel({
  kind,
  refId,
  establishmentId,
  screenId,
  userId,
  cityKey,
}: {
  kind: string | null | undefined
  refId?: string | null
  establishmentId?: string | null
  screenId?: string | null
  userId?: string | null
  cityKey?: string | null
}) {
  if (!kind) return <span className="text-text-muted">—</span>
  const label = labelOf(LOCATION_KIND_LABEL, kind)
  if (kind === 'SCREEN')
    return (
      <span>
        {label} <ScreenCode id={screenId ?? refId} />
      </span>
    )
  if (kind === 'ESTABLISHMENT')
    return (
      <span>
        {label}: <EstablishmentName id={establishmentId ?? refId} />
      </span>
    )
  if (kind === 'TECHNICIAN')
    return (
      <span>
        {label}: <UserName id={userId ?? refId} />
      </span>
    )
  if (kind === 'WAREHOUSE' || kind === 'MAINTENANCE')
    return (
      <span>
        {label}
        {cityText(cityKey) ? <span className="capitalize"> · {cityText(cityKey)}</span> : ' · central'}
      </span>
    )
  return <span>{label}</span>
}

/** UF + cidade → chave de cidade "UF:Cidade" (o backend normaliza). */
export function CityKeyFields({
  uf,
  city,
  onChange,
  legend,
  disabled,
}: {
  uf: string | null
  city: string
  onChange: (uf: string | null, city: string) => void
  legend: string
  disabled?: boolean
}) {
  return (
    <fieldset className="m-0 flex min-w-0 flex-col gap-2 border-0 p-0">
      <legend className="mb-1 text-sm font-semibold text-text">{legend}</legend>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-[160px_1fr]">
        <Field>
          <FieldLabel>UF</FieldLabel>
          <OptionSelect value={uf} onChange={(v) => onChange(v, city)} includeNone noneLabel="—" disabled={disabled} options={UFS.map((u) => ({ value: u.uf, label: u.uf }))} />
        </Field>
        <Field>
          <FieldLabel>Cidade</FieldLabel>
          <Input value={city} onChange={(e) => onChange(uf, e.target.value)} disabled={disabled || !uf} placeholder={uf ? 'Nome da cidade' : 'Escolha a UF'} />
        </Field>
      </div>
    </fieldset>
  )
}
