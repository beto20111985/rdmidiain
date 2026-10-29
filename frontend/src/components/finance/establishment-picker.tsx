/** Seletor do estabelecimento da organização (só aparece quando há mais de um). */
import { Field, FieldLabel } from '@/components/ui/field'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import type { EstablishmentOption } from './hooks'

export function EstablishmentPicker({
  items,
  selectedId,
  onSelect,
  className,
}: {
  items: EstablishmentOption[]
  selectedId: string | null | undefined
  onSelect: (id: string) => void
  className?: string
}) {
  if (items.length <= 1) return null
  return (
    <Field className={className ?? 'w-full max-w-sm'}>
      <FieldLabel>Estabelecimento</FieldLabel>
      <Select value={selectedId ?? undefined} onValueChange={onSelect}>
        <SelectTrigger>
          <SelectValue placeholder="Selecione" />
        </SelectTrigger>
        <SelectContent>
          <SelectGroup>
            {items.map((e) => (
              <SelectItem key={e.id} value={e.id}>
                {e.name}
                {e.city ? ` — ${e.city}/${e.state ?? ''}` : ''}
              </SelectItem>
            ))}
          </SelectGroup>
        </SelectContent>
      </Select>
    </Field>
  )
}
