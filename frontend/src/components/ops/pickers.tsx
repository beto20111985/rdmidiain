/**
 * Seletores com busca no servidor (escopo do usuário aplicado pelo backend):
 * estabelecimento, tela, criativo (conteúdo interno/institucional) e técnico.
 * Estrutura acessível: grupo (fieldset/legend) com campo de busca + lista de resultados rotulados.
 */
import * as React from 'react'
import { useQuery } from '@tanstack/react-query'
import { FieldDescription, FieldError, FieldLabel, Field, FieldLegend, FieldSet } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { qk, type QueryDomain } from '@/lib/queryKeys'
import { errorMessage } from '@/lib/errors'
import { creativesApi, establishmentsApi, mediaLiteApi, organizationsApi, screensApi } from '@/pages/ops/api/client'
import { useEstablishmentLookup, useScreenLookup, useTechnicians } from '@/pages/ops/api/hooks'
import { CREATIVE_KIND_LABEL } from '@/pages/ops/api/labels'
import type { CreativeLite, Establishment, MediaLite, Organization, Screen } from '@/pages/ops/api/types'
import { useCan } from '@/hooks/usePermissions'
import { useDebounced } from './use-debounced'

interface AsyncPickerProps<T> {
  legend: string
  required?: boolean
  description?: React.ReactNode
  error?: string | null
  value: string | null
  onChange: (id: string | null, item?: T) => void
  domain: QueryDomain
  queryName: string
  fetch: (search: string) => Promise<T[]>
  getId: (t: T) => string
  getLabel: (t: T) => string
  selectedLabel?: string | null
  searchLabel?: string
  disabled?: boolean
  extraKey?: string
}

function AsyncPicker<T>({
  legend,
  required,
  description,
  error,
  value,
  onChange,
  domain,
  queryName,
  fetch,
  getId,
  getLabel,
  selectedLabel,
  searchLabel = 'Buscar',
  disabled,
  extraKey = '',
}: AsyncPickerProps<T>) {
  const [search, setSearch] = React.useState('')
  const debounced = useDebounced(search, 300)
  const q = useQuery({
    queryKey: qk.sub(domain, 'picker', queryName, extraKey, debounced),
    queryFn: () => fetch(debounced),
    enabled: !disabled,
    staleTime: 60_000,
  })
  const items = q.data ?? []
  const hasSelectedInList = value ? items.some((i) => getId(i) === value) : false

  return (
    <FieldSet>
      <FieldLegend>
        {legend}
        {required ? (
          <>
            <span className="ml-1 text-text-muted" aria-hidden="true">
              *
            </span>
            <span className="sr-only"> (obrigatório)</span>
          </>
        ) : null}
      </FieldLegend>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field>
          <FieldLabel>{searchLabel}</FieldLabel>
          <Input value={search} onChange={(e) => setSearch(e.target.value)} disabled={disabled} autoComplete="off" />
        </Field>
        <Field invalid={!!error}>
          <FieldLabel>Selecionar</FieldLabel>
          <Select
            value={value ?? ''}
            onValueChange={(v) => onChange(v, items.find((i) => getId(i) === v))}
            disabled={disabled}
          >
            <SelectTrigger>
              <SelectValue placeholder={q.isLoading ? 'Carregando…' : 'Selecione'} />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                {value && !hasSelectedInList ? <SelectItem value={value}>{selectedLabel ?? 'Selecionado'}</SelectItem> : null}
                {items.map((i) => (
                  <SelectItem key={getId(i)} value={getId(i)}>
                    {getLabel(i)}
                  </SelectItem>
                ))}
                {!items.length && !q.isLoading ? (
                  <SelectItem value="__none__" disabled>
                    {q.error ? 'Não foi possível buscar' : 'Nenhum resultado'}
                  </SelectItem>
                ) : null}
              </SelectGroup>
            </SelectContent>
          </Select>
        </Field>
      </div>
      {description ? <FieldDescription>{description}</FieldDescription> : null}
      {q.error ? <p className="m-0 text-xs text-danger">{errorMessage(q.error)}</p> : null}
      <Field invalid={!!error}>
        <FieldError>{error}</FieldError>
      </Field>
    </FieldSet>
  )
}

export function EstablishmentPicker(props: {
  value: string | null
  onChange: (id: string | null, item?: Establishment) => void
  legend?: string
  required?: boolean
  error?: string | null
  description?: React.ReactNode
  disabled?: boolean
  onlyActive?: boolean
}) {
  const sel = useEstablishmentLookup(props.value)
  return (
    <AsyncPicker<Establishment>
      legend={props.legend ?? 'Estabelecimento'}
      required={props.required}
      error={props.error}
      description={props.description}
      value={props.value}
      onChange={props.onChange}
      disabled={props.disabled}
      domain="establishments"
      queryName="establishments"
      extraKey={props.onlyActive ? 'active' : 'all'}
      searchLabel="Buscar por nome ou cidade"
      fetch={(s) =>
        establishmentsApi
          .list({ search: s || undefined, page_size: 20, is_active: props.onlyActive ? true : undefined })
          .then((r) => r.items)
      }
      getId={(e) => e.id}
      getLabel={(e) => `${e.name} — ${e.city}/${e.state}`}
      selectedLabel={sel.data ? `${sel.data.name} — ${sel.data.city}/${sel.data.state}` : null}
    />
  )
}

export function OrganizationPicker(props: {
  value: string | null
  onChange: (id: string | null, item?: Organization) => void
  legend?: string
  required?: boolean
  error?: string | null
  description?: React.ReactNode
  role?: 'PARTNER' | 'ADVERTISER' | 'AGENCY' | 'FRANCHISE'
}) {
  return (
    <AsyncPicker<Organization>
      legend={props.legend ?? 'Empresa (organização)'}
      required={props.required}
      error={props.error}
      description={props.description}
      value={props.value}
      onChange={props.onChange}
      domain="organizations"
      queryName="organizations"
      extraKey={props.role ?? 'all'}
      searchLabel="Buscar empresa"
      fetch={(s) => organizationsApi.list({ search: s || undefined, role: props.role, page_size: 20 }).then((r) => r.items)}
      getId={(o) => o.id}
      getLabel={(o) => (o.trade_name && o.trade_name !== o.name ? `${o.name} (${o.trade_name})` : o.name)}
    />
  )
}

export function ScreenPicker(props: {
  value: string | null
  onChange: (id: string | null, item?: Screen) => void
  establishmentId?: string | null
  legend?: string
  required?: boolean
  error?: string | null
  description?: React.ReactNode
  disabled?: boolean
}) {
  const sel = useScreenLookup(props.value)
  return (
    <AsyncPicker<Screen>
      legend={props.legend ?? 'Tela'}
      required={props.required}
      error={props.error}
      description={props.description}
      value={props.value}
      onChange={props.onChange}
      disabled={props.disabled}
      domain="screens"
      queryName="screens"
      extraKey={props.establishmentId ?? 'all'}
      searchLabel="Buscar por código ou nome"
      fetch={(s) =>
        screensApi
          .list({ search: s || undefined, page_size: 20, establishment_id: props.establishmentId ?? undefined })
          .then((r) => r.items.filter((x) => x.status !== 'DECOMMISSIONED'))
      }
      getId={(x) => x.id}
      getLabel={(x) => `${x.friendly_id} — ${x.name}`}
      selectedLabel={sel.data ? `${sel.data.friendly_id} — ${sel.data.name}` : null}
    />
  )
}

export function CreativePicker(props: {
  value: string | null
  onChange: (id: string | null, item?: CreativeLite) => void
  kinds: Array<CreativeLite['kind']>
  legend?: string
  required?: boolean
  error?: string | null
  description?: React.ReactNode
  disabled?: boolean
}) {
  const kind = props.kinds.length === 1 ? props.kinds[0] : undefined
  return (
    <AsyncPicker<CreativeLite>
      legend={props.legend ?? 'Peça (criativo aprovado)'}
      required={props.required}
      error={props.error}
      description={props.description}
      value={props.value}
      onChange={props.onChange}
      disabled={props.disabled}
      domain="creatives"
      queryName="creatives"
      extraKey={props.kinds.join(',')}
      searchLabel="Buscar por nome ou código RD"
      fetch={(s) =>
        creativesApi
          .list({ q: s || undefined, status: 'APROVADO', kind, page_size: 25 })
          .then((r) => r.items.filter((c) => props.kinds.includes(c.kind)))
      }
      getId={(c) => c.id}
      getLabel={(c) => `${c.code} — ${c.name} (${CREATIVE_KIND_LABEL[c.kind] ?? c.kind}${c.duration_seconds ? `, ${c.duration_seconds} s` : ''})`}
    />
  )
}

/** Mídia PRONTA (READY) da biblioteca, no escopo do usuário — base para criar peças da RD. */
export function MediaPicker(props: {
  value: string | null
  onChange: (id: string | null, item?: MediaLite) => void
  /** Nome da mídia escolhida, para o caso de ela sair dos resultados da busca atual. */
  selectedLabel?: string | null
  legend?: string
  required?: boolean
  error?: string | null
  description?: React.ReactNode
  disabled?: boolean
}) {
  return (
    <AsyncPicker<MediaLite>
      selectedLabel={props.selectedLabel}
      legend={props.legend ?? 'Mídia da biblioteca (pronta)'}
      required={props.required}
      error={props.error}
      description={props.description}
      value={props.value}
      onChange={props.onChange}
      disabled={props.disabled}
      domain="media"
      queryName="ready"
      searchLabel="Buscar mídia por nome"
      fetch={(s) => mediaLiteApi.ready(s).then((r) => r.items)}
      getId={(m) => m.id}
      getLabel={(m) => {
        const ms = m.output?.duration_ms ?? m.source.duration_ms
        return `${m.name} (${m.kind === 'VIDEO' ? `vídeo${ms ? `, ${(ms / 1000).toFixed(1).replace('.', ',')} s` : ''}` : 'imagem'})`
      }}
    />
  )
}

export function TechnicianSelect({
  value,
  onChange,
  permission,
  label = 'Técnico',
  required,
  error,
  disabled,
  establishmentId,
  selectedLabel,
}: {
  value: string | null
  onChange: (id: string | null, u?: { id: string; name: string }) => void
  permission: 'maintenance.manage' | 'equipment.manage'
  label?: string
  required?: boolean
  error?: string | null
  disabled?: boolean
  /** Restringe a quem atende o estabelecimento (`GET /users/assignable?establishment_id=`). */
  establishmentId?: string | null
  /** Nome do técnico já designado, caso ele não esteja na lista atual (ex.: saiu do escopo). */
  selectedLabel?: string | null
}) {
  const can = useCan()
  const q = useTechnicians(permission, establishmentId)
  const items = q.data?.items ?? []
  const missingSelected = !!value && !q.isLoading && !items.some((u) => u.id === value)
  const allowed = can(permission)
  return (
    <Field invalid={!!error} required={required}>
      <FieldLabel>{label}</FieldLabel>
      <Select
        value={value ?? ''}
        onValueChange={(v) => onChange(v, items.find((u) => u.id === v))}
        disabled={disabled || !allowed || q.isLoading}
      >
        <SelectTrigger>
          <SelectValue placeholder={!allowed ? 'Indisponível para o seu perfil' : q.isLoading ? 'Carregando técnicos…' : 'Selecione o técnico'} />
        </SelectTrigger>
        <SelectContent>
          <SelectGroup>
            {missingSelected && value ? <SelectItem value={value}>{selectedLabel ?? 'Técnico atual'}</SelectItem> : null}
            {items.map((u) => (
              <SelectItem key={u.id} value={u.id}>
                {u.name}
              </SelectItem>
            ))}
            {!items.length && !missingSelected ? (
              <SelectItem value="__none__" disabled>
                {q.error ? 'Lista de técnicos indisponível' : 'Nenhum técnico encontrado'}
              </SelectItem>
            ) : null}
          </SelectGroup>
        </SelectContent>
      </Select>
      <FieldDescription>
        {!allowed
          ? `Atribuir técnico exige a permissão ${permission}.`
          : q.error
            ? `Não foi possível listar os técnicos: ${errorMessage(q.error)}`
            : establishmentId
              ? `Somente quem atende este estabelecimento${q.data?.truncated ? ' (lista parcial: refine pelo nome no cadastro de usuários)' : ''}.`
              : `Pessoas com a permissão ${permission}${q.data?.truncated ? ' (lista parcial)' : ''}.`}
      </FieldDescription>
      <FieldError>{error}</FieldError>
    </Field>
  )
}
