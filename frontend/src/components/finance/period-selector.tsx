/**
 * Seletor de período do DS §10: Hoje · 7 dias · 30 dias · Personalizado (datas no fuso
 * exibido). O período personalizado só é aplicado com datas válidas (erro junto ao campo).
 */
import * as React from 'react'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Field, FieldError, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { PERIOD_PRESETS, presetRange, validatePeriod, type Period, type PeriodPreset } from './period'

export interface PeriodSelectorProps {
  value: Period
  onChange: (p: Period) => void
  timeZone?: string
  /** Rótulo visível do grupo. */
  label?: string
  /** Oculta a opção "Hoje" (ex.: relatórios mensais). */
  presets?: PeriodPreset[]
  className?: string
}

export function PeriodSelector({ value, onChange, timeZone, label = 'Período', presets, className }: PeriodSelectorProps) {
  const labelId = React.useId()
  const [customOpen, setCustomOpen] = React.useState(value.preset === 'custom')
  const [start, setStart] = React.useState(value.start)
  const [end, setEnd] = React.useState(value.end)
  const [error, setError] = React.useState<string | null>(null)

  React.useEffect(() => {
    setStart(value.start)
    setEnd(value.end)
    if (value.preset === 'custom') setCustomOpen(true)
  }, [value.start, value.end, value.preset])

  const options = PERIOD_PRESETS.filter((p) => !presets || presets.includes(p.value))
  const selected = customOpen ? 'custom' : value.preset

  const pick = (v: string) => {
    if (!v) return
    if (v === 'custom') {
      setCustomOpen(true)
      return
    }
    setCustomOpen(false)
    setError(null)
    const preset = v as Exclude<PeriodPreset, 'custom'>
    onChange({ preset, ...presetRange(preset, timeZone) })
  }

  const apply = (e: React.FormEvent) => {
    e.preventDefault()
    const err = validatePeriod(start, end)
    setError(err)
    if (!err) onChange({ preset: 'custom', start, end })
  }

  return (
    <div className={cn('flex flex-col gap-3', className)}>
      <div className="flex flex-col gap-2">
        <span id={labelId} className="text-sm font-semibold text-text">
          {label}
        </span>
        <ToggleGroup type="single" value={selected} onValueChange={pick} aria-labelledby={labelId} className="self-start">
          {options.map((o) => (
            <ToggleGroupItem key={o.value} value={o.value}>
              {o.label}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
      </div>
      {customOpen ? (
        <form onSubmit={apply} noValidate className="flex flex-wrap items-end gap-3">
          <Field invalid={!!error} className="w-full max-w-[180px]">
            <FieldLabel>De</FieldLabel>
            <Input type="date" value={start} max={end || undefined} onChange={(e) => setStart(e.target.value)} />
          </Field>
          <Field invalid={!!error} className="w-full max-w-[180px]">
            <FieldLabel>Até</FieldLabel>
            <Input type="date" value={end} min={start || undefined} onChange={(e) => setEnd(e.target.value)} />
            <FieldError>{error}</FieldError>
          </Field>
          <Button type="submit" variant="secondary">
            Aplicar período
          </Button>
        </form>
      ) : null}
    </div>
  )
}
