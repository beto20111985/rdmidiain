/**
 * Seleção de registro por busca remota (organizações, estabelecimentos, campanhas,
 * criativos, marcas…). Campo de busca com rótulo + lista de resultados como botões
 * (teclado: Tab/Enter; seleção anunciada). Usado SOMENTE em telas da equipe RD quando a
 * entidade expõe nomes de estabelecimentos.
 */
import * as React from 'react'
import { useQuery } from '@tanstack/react-query'
import { Check, X } from 'lucide-react'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Spinner } from '@/components/ui/spinner'
import { qk, type QueryDomain } from '@/lib/queryKeys'
import { cn } from '@/lib/utils'
import { useDebounced } from '@/pages/campaigns/lib/hooks'
import { describeError } from '@/pages/campaigns/lib/errorText'

export interface PickerOption {
  id: string
  label: string
  description?: string
  /** Fuso IANA do registro, quando houver (estabelecimento/tela) — datas digitadas valem nele. */
  timezone?: string | null
  /** Aviso curto ao lado do nome (ex.: estabelecimento fora do inventário) — visível na lista e quando escolhido. */
  hint?: string | null
}

export interface EntityPickerProps {
  label: string
  domain: QueryDomain
  /** Busca no servidor (texto digitado → opções). */
  search: (text: string) => Promise<PickerOption[]>
  value: PickerOption | null
  onChange: (v: PickerOption | null) => void
  description?: string
  error?: string
  required?: boolean
  disabled?: boolean
  placeholder?: string
  minChars?: number
}

export function EntityPicker({
  label,
  domain,
  search,
  value,
  onChange,
  description,
  error,
  required,
  disabled,
  placeholder = 'Digite para buscar',
  minChars = 0,
}: EntityPickerProps) {
  const [text, setText] = React.useState('')
  const debounced = useDebounced(text, 350)
  const listId = React.useId()
  const enabled = !value && !disabled && debounced.trim().length >= minChars
  const q = useQuery({
    queryKey: qk.sub(domain, 'picker', label, debounced.trim()),
    queryFn: () => search(debounced.trim()),
    enabled,
    staleTime: 60 * 1000,
  })

  if (value) {
    return (
      <Field invalid={!!error} required={required}>
        <FieldLabel required={required}>{label}</FieldLabel>
        <div className="flex min-h-11 items-center justify-between gap-2 rounded-md border border-control-border bg-field px-3">
          <span className="min-w-0 truncate text-sm">
            <span className="font-semibold">{value.label}</span>
            {value.description ? <span className="text-text-muted"> · {value.description}</span> : null}
            {value.hint ? <span className="text-warning"> · {value.hint}</span> : null}
          </span>
          <Button variant="ghost" size="icon" aria-label={`Trocar ${label.toLowerCase()}`} onClick={() => onChange(null)} disabled={disabled}>
            <X aria-hidden="true" />
          </Button>
        </div>
        {description ? <FieldDescription>{description}</FieldDescription> : null}
        <FieldError>{error}</FieldError>
      </Field>
    )
  }

  const options = q.data ?? []
  return (
    <Field invalid={!!error} required={required}>
      <FieldLabel required={required}>{label}</FieldLabel>
      <Input
        type="search"
        value={text}
        placeholder={placeholder}
        onChange={(e) => setText(e.target.value)}
        aria-controls={listId}
        autoComplete="off"
        disabled={disabled}
      />
      {description ? <FieldDescription>{description}</FieldDescription> : null}
      <FieldError>{error}</FieldError>
      <div id={listId} aria-live="polite" className="flex flex-col gap-1">
        {!enabled ? null : q.isLoading ? (
          <span className="inline-flex items-center gap-2 text-xs text-text-muted">
            <Spinner className="size-4" /> Buscando…
          </span>
        ) : q.error ? (
          <span className="text-xs text-danger">{describeError(q.error)}</span>
        ) : options.length === 0 ? (
          <span className="text-xs text-text-muted">Nenhum resultado.</span>
        ) : (
          <ul className="m-0 flex max-h-56 list-none flex-col gap-1 overflow-y-auto p-0" aria-label={`Resultados para ${label}`}>
            {options.slice(0, 20).map((o) => (
              <li key={o.id}>
                <button
                  type="button"
                  onClick={() => {
                    onChange(o)
                    setText('')
                  }}
                  className={cn(
                    'flex min-h-11 w-full items-center gap-2 rounded-md px-3 text-left text-sm hover:bg-surface-hover focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus',
                  )}
                >
                  <Check aria-hidden="true" className="size-4 opacity-0" />
                  <span className="min-w-0 truncate">
                    <span className="font-semibold">{o.label}</span>
                    {o.description ? <span className="text-text-muted"> · {o.description}</span> : null}
                    {o.hint ? <span className="text-warning"> · {o.hint}</span> : null}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Field>
  )
}
