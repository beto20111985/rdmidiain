/** Filtro de lista por valor único ("Todos" limpa o filtro) — estado na URL via useTableState. */
import { Field, FieldLabel } from '@/components/ui/field'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

export function FilterSelect({
  label,
  value,
  onChange,
  options,
  allLabel = 'Todos',
}: {
  label: string
  value: string | undefined
  onChange: (v: string | null) => void
  options: Array<{ value: string; label: string }>
  allLabel?: string
}) {
  return (
    <Field className="w-48">
      <FieldLabel>{label}</FieldLabel>
      <Select value={value ?? '__all'} onValueChange={(v) => onChange(v === '__all' ? null : v)}>
        <SelectTrigger>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectGroup>
            <SelectItem value="__all">{allLabel}</SelectItem>
            {options.map((o) => (
              <SelectItem key={o.value} value={o.value}>
                {o.label}
              </SelectItem>
            ))}
          </SelectGroup>
        </SelectContent>
      </Select>
    </Field>
  )
}
