/**
 * Seletores com busca no servidor (empresas e estabelecimentos), acessíveis por teclado:
 * campo de busca + lista de resultados (Select). Os resultados vêm da API, filtrados pelo
 * escopo do usuário no backend.
 */
import * as React from 'react'
import { useQuery, keepPreviousData } from '@tanstack/react-query'
import { http, type Paginated } from '@/lib/http'
import { Input } from '@/components/ui/input'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { errorMessage } from '@/lib/errors'
import { useDebounced } from './hooks'

export interface PickedEntity {
  id: string
  label: string
}

interface BasePickerProps {
  label: string
  value: PickedEntity | null
  onChange: (v: PickedEntity | null) => void
  description?: React.ReactNode
  error?: string
  required?: boolean
  disabled?: boolean
}

interface OrgRow {
  id: string
  name: string
  trade_name: string | null
  document_formatted?: string
  business_roles?: string[]
}

export function OrgPicker({ role, ...props }: BasePickerProps & { role?: 'ADVERTISER' | 'PARTNER' | 'AGENCY' | 'FRANCHISE' }) {
  const [search, setSearch] = React.useState('')
  const debounced = useDebounced(search)
  const q = useQuery({
    queryKey: ['organizations', 'picker', { search: debounced, role }],
    queryFn: () =>
      http.get<Paginated<OrgRow>>('/organizations', {
        query: { search: debounced || undefined, role, is_active: true, page: 1, page_size: 20, sort: 'name' },
      }),
    placeholderData: keepPreviousData,
    enabled: !props.disabled,
  })
  const options = (q.data?.items ?? []).map((o) => ({
    id: o.id,
    label: `${o.trade_name || o.name}${o.document_formatted ? ` · ${o.document_formatted}` : ''}`,
  }))
  return <PickerBody {...props} search={search} setSearch={setSearch} options={options} loading={q.isFetching} error={props.error ?? (q.error ? errorMessage(q.error) : undefined)} searchLabel="Buscar empresa (nome ou CPF/CNPJ)" />
}

interface EstRow {
  id: string
  name: string
  trade_name: string | null
  city: string | null
  state: string | null
}

export function EstablishmentPicker({ orgId, ...props }: BasePickerProps & { orgId?: string | null }) {
  const [search, setSearch] = React.useState('')
  const debounced = useDebounced(search)
  const q = useQuery({
    queryKey: ['establishments', 'picker', { search: debounced, orgId }],
    queryFn: () =>
      http.get<Paginated<EstRow>>('/establishments', {
        query: { search: debounced || undefined, org_id: orgId ?? undefined, page: 1, page_size: 20 },
      }),
    placeholderData: keepPreviousData,
    enabled: !props.disabled,
  })
  const options = (q.data?.items ?? []).map((e) => ({
    id: e.id,
    label: `${e.trade_name || e.name}${e.city ? ` · ${e.city}/${e.state ?? ''}` : ''}`,
  }))
  return <PickerBody {...props} search={search} setSearch={setSearch} options={options} loading={q.isFetching} error={props.error ?? (q.error ? errorMessage(q.error) : undefined)} searchLabel="Buscar estabelecimento" />
}

function PickerBody({
  label,
  value,
  onChange,
  description,
  error,
  required,
  disabled,
  search,
  setSearch,
  options,
  loading,
  searchLabel,
}: BasePickerProps & {
  search: string
  setSearch: (s: string) => void
  options: PickedEntity[]
  loading: boolean
  searchLabel: string
}) {
  // Mantém a opção escolhida visível mesmo se a busca mudar.
  const all = value && !options.some((o) => o.id === value.id) ? [value, ...options] : options
  return (
    <div className="flex flex-col gap-3">
      <Field disabled={disabled}>
        <FieldLabel>{searchLabel}</FieldLabel>
        <Input type="search" value={search} onChange={(e) => setSearch(e.target.value)} autoComplete="off" />
      </Field>
      <Field invalid={!!error} required={required} disabled={disabled}>
        <FieldLabel>{label}</FieldLabel>
        <Select
          value={value?.id ?? ''}
          // '' do <select> nativo (opção ainda não registrada) é filtrado no Select da fundação.
          onValueChange={(id) => onChange(all.find((o) => o.id === id) ?? null)}
          disabled={disabled}
        >
          <SelectTrigger>
            <SelectValue placeholder={loading ? 'Carregando…' : all.length ? 'Selecione' : 'Nenhum resultado'} />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              {all.map((o) => (
                <SelectItem key={o.id} value={o.id}>
                  {o.label}
                </SelectItem>
              ))}
            </SelectGroup>
          </SelectContent>
        </Select>
        {description ? <FieldDescription>{description}</FieldDescription> : null}
        <FieldError>{error}</FieldError>
      </Field>
    </div>
  )
}
