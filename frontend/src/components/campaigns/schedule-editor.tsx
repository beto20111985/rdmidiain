/**
 * Período e horários da campanha: datas, período (avulso/mensal/semestral/anual — sem
 * trimestral), dias da semana, faixas horárias (alinhadas à hora), frequência por hora/dia
 * ou horários específicos, intervalo mínimo, prioridade/premium, exclusividade de
 * concorrente. Validação prévia no cliente; o servidor revalida e devolve o erro por campo.
 */
import { Plus, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Field, FieldDescription, FieldError, FieldLabel, FieldLegend, FieldSet } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { EXCLUSIVITY_LABEL, FREQ_LABEL, PERIOD_LABEL, PRIORITY_LABEL, WEEKDAYS } from '@/pages/campaigns/lib/labels'
import { daysFromMask, hourLabel, maskFromDays } from '@/pages/campaigns/lib/rules'
import type { Exclusivity, FreqKind, PeriodType, Priority } from '@/pages/campaigns/lib/types'

export interface ScheduleValue {
  period_type: PeriodType
  period_days: number | null
  start_date: string
  end_date: string
  days_mask: number
  time_bands: number[][]
  freq_kind: FreqKind
  freq_value: number
  specific_times: string[]
  min_interval_s: number
  priority: Priority
  exclusivity: Exclusivity
  allow_same_advertiser_consecutive: boolean
  auto_renew: boolean
}

const HOURS = Array.from({ length: 25 }, (_, h) => h * 60)

export function ScheduleEditor({
  value,
  onChange,
  errors = {},
  disabled,
  showDates = true,
}: {
  value: ScheduleValue
  onChange: (patch: Partial<ScheduleValue>) => void
  errors?: Record<string, string | undefined>
  disabled?: boolean
  showDates?: boolean
}) {
  const days = daysFromMask(value.days_mask)
  const recurring = value.period_type !== 'AVULSO'

  return (
    <div className="flex flex-col gap-6">
      {showDates ? (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          <Field invalid={!!errors.period_type}>
            <FieldLabel required>Período</FieldLabel>
            <Select value={value.period_type} onValueChange={(v) => onChange({ period_type: v as PeriodType })} disabled={disabled}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  {(Object.keys(PERIOD_LABEL) as PeriodType[]).map((p) => (
                    <SelectItem key={p} value={p}>
                      {PERIOD_LABEL[p]}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
            <FieldError>{errors.period_type}</FieldError>
          </Field>
          <Field invalid={!!errors.start_date}>
            <FieldLabel required>Início</FieldLabel>
            <Input type="date" value={value.start_date} onChange={(e) => onChange({ start_date: e.target.value })} disabled={disabled} />
            <FieldError>{errors.start_date}</FieldError>
          </Field>
          {recurring ? (
            <Field>
              <FieldLabel>Fim</FieldLabel>
              <Input value={value.end_date ? value.end_date.split('-').reverse().join('/') : 'Calculado pelo servidor'} readOnly disabled />
              <FieldDescription>Planos mensal/semestral/anual: o fim é calculado pelo servidor a partir do início.</FieldDescription>
            </Field>
          ) : (
            <Field invalid={!!errors.end_date}>
              <FieldLabel required>Fim</FieldLabel>
              <Input type="date" value={value.end_date} min={value.start_date || undefined} onChange={(e) => onChange({ end_date: e.target.value })} disabled={disabled} />
              <FieldDescription>A campanha vale até 23:59:59 do último dia no fuso de cada tela.</FieldDescription>
              <FieldError>{errors.end_date}</FieldError>
            </Field>
          )}
        </div>
      ) : null}

      <FieldSet>
        <FieldLegend>Dias da semana</FieldLegend>
        <div className="flex flex-wrap gap-x-4">
          {WEEKDAYS.map((d) => (
            <Field key={d.bit} orientation="horizontal" className="min-h-11 w-auto">
              <Checkbox
                checked={days.includes(d.bit)}
                disabled={disabled}
                onCheckedChange={(c) => {
                  const next = c ? [...days, d.bit] : days.filter((b) => b !== d.bit)
                  onChange({ days_mask: maskFromDays(next) })
                }}
              />
              <FieldLabel className="font-normal">
                <span aria-hidden="true">{d.short}</span>
                <span className="sr-only">{d.label}</span>
              </FieldLabel>
            </Field>
          ))}
        </div>
        {errors.days_mask || days.length === 0 ? <FieldError>{errors.days_mask ?? 'Selecione ao menos um dia.'}</FieldError> : null}
      </FieldSet>

      <FieldSet>
        <FieldLegend>Faixas horárias (hora local de cada tela)</FieldLegend>
        <ul className="m-0 flex list-none flex-col gap-2 p-0">
          {value.time_bands.map((b, i) => (
            <li key={i} className="flex flex-wrap items-end gap-3">
              {(['De', 'Até'] as const).map((lbl, k) => (
                <Field key={lbl} className="w-36">
                  <FieldLabel>
                    {lbl}
                    <span className="sr-only"> (faixa {i + 1})</span>
                  </FieldLabel>
                  <Select
                    value={String(b[k])}
                    disabled={disabled}
                    onValueChange={(v) => {
                      const next = value.time_bands.map((x) => [...x])
                      next[i][k] = Number(v)
                      onChange({ time_bands: next })
                    }}
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectGroup>
                        {HOURS.filter((m) => (k === 0 ? m < 1440 : m > 0)).map((m) => (
                          <SelectItem key={m} value={String(m)}>
                            {hourLabel(m)}
                          </SelectItem>
                        ))}
                      </SelectGroup>
                    </SelectContent>
                  </Select>
                </Field>
              ))}
              <Button
                variant="ghost"
                size="icon"
                aria-label={`Remover faixa ${hourLabel(b[0])}–${hourLabel(b[1])}`}
                disabled={disabled || value.time_bands.length <= 1}
                onClick={() => onChange({ time_bands: value.time_bands.filter((_, j) => j !== i) })}
              >
                <Trash2 aria-hidden="true" />
              </Button>
            </li>
          ))}
        </ul>
        <Button
          variant="tertiary"
          className="w-fit"
          disabled={disabled || value.time_bands.length >= 24}
          onClick={() => onChange({ time_bands: [...value.time_bands, [480, 1080]] })}
        >
          <Plus aria-hidden="true" />
          Adicionar faixa
        </Button>
        <FieldError>{errors.time_bands}</FieldError>
      </FieldSet>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <Field invalid={!!errors.freq_kind}>
          <FieldLabel required>Frequência</FieldLabel>
          <Select
            value={value.freq_kind}
            disabled={disabled}
            onValueChange={(v) => {
              const fk = v as FreqKind
              onChange(fk === 'SPECIFIC_TIME' ? { freq_kind: fk, freq_value: Math.max(1, value.specific_times.length) } : { freq_kind: fk })
            }}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                {(Object.keys(FREQ_LABEL) as FreqKind[]).map((k) => (
                  <SelectItem key={k} value={k}>
                    {FREQ_LABEL[k]}
                  </SelectItem>
                ))}
              </SelectGroup>
            </SelectContent>
          </Select>
        </Field>
        {value.freq_kind !== 'SPECIFIC_TIME' ? (
          <Field invalid={!!errors.freq_value}>
            <FieldLabel required>{value.freq_kind === 'PER_HOUR' ? 'Inserções por hora' : 'Inserções por dia'}</FieldLabel>
            <Input
              type="number"
              min={1}
              max={1440}
              value={value.freq_value}
              disabled={disabled}
              onChange={(e) => onChange({ freq_value: Math.max(0, Math.floor(Number(e.target.value) || 0)) })}
            />
            <FieldError>{errors.freq_value}</FieldError>
          </Field>
        ) : (
          <Field invalid={!!errors.specific_times}>
            <FieldLabel required>Horários específicos (HH:MM)</FieldLabel>
            <Input
              value={value.specific_times.join(', ')}
              placeholder="08:00, 12:30, 18:00"
              disabled={disabled}
              onChange={(e) => {
                const times = e.target.value
                  .split(/[,;\s]+/)
                  .map((s) => s.trim())
                  .filter(Boolean)
                onChange({ specific_times: times, freq_value: Math.max(1, times.length) })
              }}
            />
            <FieldDescription>Separe por vírgula. Precisão de ± 1 ciclo de rotação, no fuso da tela.</FieldDescription>
            <FieldError>{errors.specific_times}</FieldError>
          </Field>
        )}
        <Field invalid={!!errors.min_interval_s}>
          <FieldLabel>Intervalo mínimo entre exibições (min)</FieldLabel>
          <Input
            type="number"
            min={0}
            max={1440}
            value={Math.round(value.min_interval_s / 60)}
            disabled={disabled}
            onChange={(e) => onChange({ min_interval_s: Math.max(0, Math.floor(Number(e.target.value) || 0)) * 60 })}
          />
          <FieldError>{errors.min_interval_s}</FieldError>
        </Field>
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <div className="flex flex-col gap-2">
          <span id="priority-label" className="text-sm font-semibold">
            Prioridade
          </span>
          <ToggleGroup
            type="single"
            value={value.priority}
            onValueChange={(v) => v && onChange({ priority: v as Priority })}
            aria-labelledby="priority-label"
            disabled={disabled}
          >
            {(Object.keys(PRIORITY_LABEL) as Priority[]).map((p) => (
              <ToggleGroupItem key={p} value={p}>
                {PRIORITY_LABEL[p]}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        </div>
        <div className="flex flex-col gap-2">
          <span id="exclusivity-label" className="text-sm font-semibold">
            Exclusividade
          </span>
          <ToggleGroup
            type="single"
            value={value.exclusivity}
            onValueChange={(v) => v && onChange({ exclusivity: v as Exclusivity })}
            aria-labelledby="exclusivity-label"
            disabled={disabled}
          >
            {(Object.keys(EXCLUSIVITY_LABEL) as Exclusivity[]).map((p) => (
              <ToggleGroupItem key={p} value={p}>
                {EXCLUSIVITY_LABEL[p]}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
          <p className="m-0 text-xs text-text-muted">Concorrentes nunca aparecem no mesmo bloco, com ou sem exclusividade.</p>
        </div>
      </div>

      <div className="flex flex-col gap-1">
        <Field orientation="horizontal" className="min-h-11">
          <Switch
            checked={value.allow_same_advertiser_consecutive}
            disabled={disabled}
            onCheckedChange={(c) => onChange({ allow_same_advertiser_consecutive: c })}
          />
          <FieldLabel className="font-normal">Permitir meus anúncios em sequência no mesmo bloco</FieldLabel>
        </Field>
        {recurring ? (
          <Field orientation="horizontal" className="min-h-11">
            <Switch checked={value.auto_renew} disabled={disabled} onCheckedChange={(c) => onChange({ auto_renew: c })} />
            <FieldLabel className="font-normal">Renovação automática (cobrança PIX gerada antes do vencimento)</FieldLabel>
          </Field>
        ) : null}
      </div>
    </div>
  )
}
