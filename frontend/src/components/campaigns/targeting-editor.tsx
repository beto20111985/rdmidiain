/**
 * Segmentação da campanha por local (nunca por TV individual): país, região, UF, cidade,
 * bairro, CEP, categoria de estabelecimento e — SOMENTE para a equipe RD — estabelecimentos
 * específicos; cada alvo pode incluir ou excluir. Semântica do servidor: E entre tipos,
 * OU dentro do mesmo tipo; estabelecimento específico soma; exclusões removem.
 * O anunciante NUNCA vê nomes de estabelecimentos (a opção nem é oferecida).
 */
import * as React from 'react'
import { Plus, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Badge } from '@/components/ui/badge'
import { EntityPicker, type PickerOption } from './entity-picker'
import { establishmentInventoryHint, REGIONS, TARGET_LABEL, UFS } from '@/pages/campaigns/lib/labels'
import { targetText } from '@/pages/campaigns/lib/format'
import { lookupApi } from '@/pages/campaigns/lib/api'
import type { Target, TargetType, TreeItem } from '@/pages/campaigns/lib/types'

const ALL_TYPES: TargetType[] = ['COUNTRY', 'REGION', 'STATE', 'CITY', 'NEIGHBORHOOD', 'POSTAL_CODE', 'CATEGORY', 'ESTABLISHMENT']

export interface TargetingEditorProps {
  targets: Target[]
  onChange: (targets: Target[]) => void
  isStaff: boolean
  categories: TreeItem[]
  disabled?: boolean
  error?: string
}

export function TargetingEditor({ targets, onChange, isStaff, categories, disabled, error }: TargetingEditorProps) {
  const types = isStaff ? ALL_TYPES : ALL_TYPES.filter((t) => t !== 'ESTABLISHMENT')
  const [type, setType] = React.useState<TargetType>('STATE')
  const [mode, setMode] = React.useState<'include' | 'exclude'>('include')
  const [uf, setUf] = React.useState<string>('')
  const [city, setCity] = React.useState('')
  const [hood, setHood] = React.useState('')
  const [text, setText] = React.useState('')
  const [choice, setChoice] = React.useState('')
  const [est, setEst] = React.useState<PickerOption | null>(null)
  const [names, setNames] = React.useState<Record<string, string>>({})
  const [localError, setLocalError] = React.useState<string | null>(null)

  const categoryNames = React.useMemo(() => Object.fromEntries(categories.map((c) => [c.id, c.name])), [categories])
  const allNames = { ...categoryNames, ...names }

  const reset = () => {
    setUf('')
    setCity('')
    setHood('')
    setText('')
    setChoice('')
    setEst(null)
  }

  const build = (): Target | string => {
    const exclude = mode === 'exclude'
    switch (type) {
      case 'COUNTRY':
        return { type, value: 'BRA', exclude }
      case 'REGION':
      case 'STATE':
      case 'CATEGORY':
        return choice ? { type, value: choice, exclude } : 'Escolha uma opção.'
      case 'CITY':
        if (!uf || city.trim().length < 2) return 'Informe a UF e o nome da cidade.'
        return { type, value: `${uf}:${city.trim()}`, exclude }
      case 'NEIGHBORHOOD':
        if (!uf || city.trim().length < 2 || hood.trim().length < 2) return 'Informe UF, cidade e bairro.'
        return { type, value: `${uf}:${city.trim()}:${hood.trim()}`, exclude }
      case 'POSTAL_CODE': {
        const d = text.replace(/\D/g, '')
        if (d.length < 2 || d.length > 8) return 'CEP (ou prefixo) deve ter de 2 a 8 dígitos.'
        return { type, value: d, exclude }
      }
      case 'ESTABLISHMENT':
        return est ? { type, value: est.id, exclude } : 'Escolha o estabelecimento.'
    }
  }

  const add = () => {
    const t = build()
    if (typeof t === 'string') {
      setLocalError(t)
      return
    }
    if (targets.some((x) => x.type === t.type && x.value.toLowerCase() === t.value.toLowerCase() && x.exclude === t.exclude)) {
      setLocalError('Este alvo já foi adicionado.')
      return
    }
    if (t.type === 'ESTABLISHMENT' && est) setNames((n) => ({ ...n, [est.id]: est.label }))
    setLocalError(null)
    onChange([...targets, t])
    reset()
  }

  const includes = targets.filter((t) => !t.exclude)

  return (
    <div className="flex flex-col gap-4">
      <fieldset className="m-0 flex min-w-0 flex-col gap-4 rounded-lg border border-border p-4" disabled={disabled}>
        <legend className="px-1 text-sm font-semibold">Adicionar segmentação</legend>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Field>
            <FieldLabel>Tipo</FieldLabel>
            <Select
              value={type}
              onValueChange={(v) => {
                setType(v as TargetType)
                reset()
                setLocalError(null)
              }}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  {types.map((t) => (
                    <SelectItem key={t} value={t}>
                      {TARGET_LABEL[t]}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
          </Field>
          <div className="flex flex-col gap-2">
            <span className="text-sm font-semibold" id="target-mode-label">
              Efeito
            </span>
            <ToggleGroup
              type="single"
              value={mode}
              onValueChange={(v) => v && setMode(v as 'include' | 'exclude')}
              aria-labelledby="target-mode-label"
            >
              <ToggleGroupItem value="include">Incluir</ToggleGroupItem>
              <ToggleGroupItem value="exclude">Excluir</ToggleGroupItem>
            </ToggleGroup>
          </div>
        </div>

        {type === 'COUNTRY' ? <p className="m-0 text-sm text-text-muted">Brasil (único país atendido).</p> : null}
        {type === 'REGION' || type === 'STATE' || type === 'CATEGORY' ? (
          <Field>
            <FieldLabel>{TARGET_LABEL[type]}</FieldLabel>
            <Select value={choice} onValueChange={setChoice}>
              <SelectTrigger>
                <SelectValue placeholder="Selecione" />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  {type === 'REGION'
                    ? REGIONS.map((r) => (
                        <SelectItem key={r.value} value={r.value}>
                          {r.label}
                        </SelectItem>
                      ))
                    : type === 'STATE'
                      ? UFS.map((u) => (
                          <SelectItem key={u} value={u}>
                            {u}
                          </SelectItem>
                        ))
                      : categories.map((c) => (
                          <SelectItem key={c.id} value={c.id}>
                            {'— '.repeat(c.depth)}
                            {c.name}
                          </SelectItem>
                        ))}
                </SelectGroup>
              </SelectContent>
            </Select>
            {type === 'CATEGORY' && !categories.length ? (
              <FieldDescription>Nenhuma categoria cadastrada pela RD.</FieldDescription>
            ) : null}
          </Field>
        ) : null}
        {type === 'CITY' || type === 'NEIGHBORHOOD' ? (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
            <Field>
              <FieldLabel>UF</FieldLabel>
              <Select value={uf} onValueChange={setUf}>
                <SelectTrigger>
                  <SelectValue placeholder="UF" />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    {UFS.map((u) => (
                      <SelectItem key={u} value={u}>
                        {u}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
            </Field>
            <Field>
              <FieldLabel>Cidade</FieldLabel>
              <Input value={city} onChange={(e) => setCity(e.target.value)} placeholder="Ex.: Goiânia" />
            </Field>
            {type === 'NEIGHBORHOOD' ? (
              <Field>
                <FieldLabel>Bairro</FieldLabel>
                <Input value={hood} onChange={(e) => setHood(e.target.value)} />
              </Field>
            ) : null}
          </div>
        ) : null}
        {type === 'POSTAL_CODE' ? (
          <Field>
            <FieldLabel>CEP ou prefixo</FieldLabel>
            <Input inputMode="numeric" value={text} onChange={(e) => setText(e.target.value)} placeholder="Ex.: 74 ou 74000-000" />
            <FieldDescription>De 2 a 8 dígitos (o prefixo cobre a faixa inteira).</FieldDescription>
          </Field>
        ) : null}
        {type === 'ESTABLISHMENT' && isStaff ? (
          <EntityPicker
            label="Estabelecimento"
            domain="establishments"
            value={est}
            onChange={setEst}
            description="Visível somente para a equipe RD."
            search={async (q) => {
              const r = await lookupApi.establishments({ search: q || undefined, page_size: 20, is_active: true })
              return r.items.map((e) => ({
                id: e.id,
                label: e.name,
                description: [e.city, e.state].filter(Boolean).join(' · '),
                hint: establishmentInventoryHint(e),
              }))
            }}
          />
        ) : null}
        {localError ? (
          <p role="alert" className="m-0 text-sm text-danger">
            {localError}
          </p>
        ) : null}
        <Button variant="secondary" onClick={add} className="w-fit">
          <Plus aria-hidden="true" />
          Adicionar
        </Button>
      </fieldset>

      <div className="flex flex-col gap-2">
        <h3 className="text-title-card">Segmentação escolhida</h3>
        {targets.length === 0 ? (
          <p className="m-0 text-sm text-text-muted">Nenhum alvo ainda. Inclua ao menos um local ou categoria — nunca vendemos “todas as telas” por omissão.</p>
        ) : (
          <ul className="m-0 flex list-none flex-col gap-2 p-0" aria-label="Alvos da segmentação">
            {targets.map((t, i) => (
              <li key={`${t.type}-${t.value}-${t.exclude}`} className="flex items-center justify-between gap-2 rounded-lg border border-border px-3 py-1">
                <span className="flex min-w-0 items-center gap-2 text-sm">
                  <Badge tone={t.exclude ? 'danger' : 'info'}>{t.exclude ? 'Exclui' : 'Inclui'}</Badge>
                  <span className="truncate">{targetText(t, allNames)}</span>
                </span>
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label={`Remover: ${targetText(t, allNames)}`}
                  disabled={disabled}
                  onClick={() => onChange(targets.filter((_, j) => j !== i))}
                >
                  <Trash2 aria-hidden="true" />
                </Button>
              </li>
            ))}
          </ul>
        )}
        {targets.length > 0 && includes.length === 0 ? (
          <p className="m-0 text-sm text-warning">Só há exclusões: inclua ao menos um local ou categoria.</p>
        ) : null}
        {error ? <FieldError>{error}</FieldError> : null}
      </div>
    </div>
  )
}
