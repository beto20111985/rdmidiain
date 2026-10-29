/** Select de filtro rotulado com opção "Todos" (Radix não aceita valor vazio → sentinela). */
import { Field, FieldLabel } from '@/components/ui/field'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { cn } from '@/lib/utils'

const ALL = '__all__'

export interface Option {
  value: string
  label: string
}

export function FilterSelect({
  label,
  value,
  onChange,
  options,
  allLabel = 'Todos',
  className,
  disabled,
}: {
  label: string
  value: string | null | undefined
  onChange: (value: string | null) => void
  options: Option[]
  allLabel?: string
  className?: string
  disabled?: boolean
}) {
  return (
    <Field className={cn('w-full sm:w-48', className)}>
      <FieldLabel>{label}</FieldLabel>
      <Select
        value={value || ALL}
        onValueChange={(v) => onChange(v === ALL ? null : v)}
        disabled={disabled}
      >
        <SelectTrigger>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectGroup>
            <SelectItem value={ALL}>{allLabel}</SelectItem>
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

/** Select simples de formulário (opções fixas), controlado. */
export function OptionSelect({
  value,
  onChange,
  options,
  placeholder = 'Selecione',
  disabled,
  includeNone,
  noneLabel = 'Nenhum',
}: {
  value: string | null | undefined
  onChange: (value: string | null) => void
  options: Option[]
  placeholder?: string
  disabled?: boolean
  includeNone?: boolean
  noneLabel?: string
}) {
  return (
    <Select
      // '' = sem valor (mostra o placeholder) e mantém o Select sempre controlado; o '' emitido pelo
      // <select> nativo antes de a opção existir é filtrado no Select da fundação.
      value={value ? value : includeNone ? ALL : ''}
      onValueChange={(v) => onChange(v === ALL ? null : v)}
      disabled={disabled}
    >
      <SelectTrigger>
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent>
        <SelectGroup>
          {includeNone ? <SelectItem value={ALL}>{noneLabel}</SelectItem> : null}
          {options.map((o) => (
            <SelectItem key={o.value} value={o.value}>
              {o.label}
            </SelectItem>
          ))}
        </SelectGroup>
      </SelectContent>
    </Select>
  )
}
