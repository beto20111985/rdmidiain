/**
 * Tabelas de preço versionadas (REQ-PLN-002/003): a ATIVA é imutável — para mudar preço,
 * duplica-se e ativa-se a nova versão (o servidor bloqueia se algum plano ativo ficar com
 * margem negativa). Itens: duração, frequência, faixas de telas, período, região/cidade,
 * faixa horária, classe/premium, exclusividade, preço mínimo, taxas, impostos e custos.
 */
import * as React from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Copy, Pencil, Plus, Rocket, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Skeleton } from '@/components/ui/skeleton'
import { ConfirmDialog } from '@/components/patterns/confirm-dialog'
import { EmptyState, ErrorState } from '@/components/patterns/states'
import { ToneBadge } from '@/components/campaigns/badges'
import { qk } from '@/lib/queryKeys'
import { isApiError } from '@/lib/errors'
import { formatBps, formatDateTime } from '@/lib/format'
import { cn } from '@/lib/utils'
import { pricingApi } from '../lib/api'
import { PRICE_ITEM_KEY_HINT, PRICE_ITEM_KIND_LABEL, PRICE_ITEM_MODES, PRICE_MODE_LABEL } from '../lib/labels'
import { itemValueLabel, itemValueText, itemValueToInput, parseItemValue } from '../lib/money'
import type { PriceItem, PriceTable } from '../lib/types'
import { describeError, isPriceFloorError } from '@/pages/campaigns/lib/errorText'

const TABLE_TONE = { RASCUNHO: 'neutral', ATIVA: 'success', ARQUIVADA: 'neutral' } as const
const TABLE_LABEL = { RASCUNHO: 'Rascunho', ATIVA: 'Ativa', ARQUIVADA: 'Arquivada' } as const
const RANGE_KINDS = ['FREQUENCY', 'SCREEN_COUNT', 'TIME_BAND']

export function TablesTab({ canManage }: { canManage: boolean }) {
  const qc = useQueryClient()
  const list = useQuery({ queryKey: qk.sub('pricing', 'tables'), queryFn: pricingApi.tables })
  const [selectedId, setSelectedId] = React.useState<string | null>(null)
  const tables = list.data?.items ?? []
  const current = selectedId ?? tables.find((t) => t.status === 'ATIVA')?.id ?? tables[0]?.id ?? null
  const detail = useQuery({ queryKey: qk.detail('pricing', current ?? '-'), queryFn: () => pricingApi.table(current as string), enabled: !!current })
  const [creating, setCreating] = React.useState(false)
  const [activating, setActivating] = React.useState(false)
  const [activationResult, setActivationResult] = React.useState<PriceTable | null>(null)
  const [itemEditing, setItemEditing] = React.useState<PriceItem | 'new' | null>(null)
  const [deleting, setDeleting] = React.useState<PriceItem | null>(null)
  const refresh = () => void qc.invalidateQueries({ queryKey: qk.root('pricing') })
  const t = detail.data
  const draft = t?.status === 'RASCUNHO'

  const duplicate = async (id: string) => {
    try {
      const n = await pricingApi.duplicateTable(id)
      toast.success(`Versão ${n.version} criada como rascunho.`)
      setSelectedId(n.id)
      refresh()
    } catch (e) {
      toast.error(describeError(e))
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-title-section">Tabelas de preço</h2>
        {canManage ? (
          <Button onClick={() => setCreating(true)}>
            <Plus aria-hidden="true" />
            Nova tabela
          </Button>
        ) : null}
      </div>
      {list.isLoading ? (
        <Skeleton className="h-32 w-full" />
      ) : list.error ? (
        <ErrorState error={list.error} onRetry={() => void list.refetch()} size="compact" />
      ) : tables.length === 0 ? (
        <EmptyState
          size="compact"
          headingLevel="h3"
          title="Nenhuma tabela de preço"
          description="Sem tabela ATIVA o checkout fica indisponível. Crie uma tabela com o preço-base por duração e ative."
        />
      ) : (
        <ul className="m-0 flex list-none flex-wrap gap-2 p-0" aria-label="Versões da tabela de preços">
          {tables.map((x) => (
            <li key={x.id}>
              <button
                type="button"
                onClick={() => setSelectedId(x.id)}
                aria-pressed={x.id === current}
                className={cn(
                  'flex min-h-11 flex-col items-start rounded-lg border px-3 py-2 text-left text-sm',
                  x.id === current ? 'border-link bg-surface-hover' : 'border-border hover:bg-surface-hover',
                )}
              >
                <span className="font-semibold">
                  v{x.version} · {x.name}
                </span>
                <span className="flex items-center gap-2 text-xs text-text-muted">
                  <ToneBadge label={TABLE_LABEL[x.status]} tone={TABLE_TONE[x.status]} />
                  {x.activated_at ? `ativada ${formatDateTime(x.activated_at)}` : `criada ${formatDateTime(x.created_at)}`}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {activationResult?.warnings?.length ? (
        <Alert variant="warning">
          <AlertTitle>Tabela ativada com alerta de margem</AlertTitle>
          <AlertDescription>
            {activationResult.warnings.map((w) => `${w.name ?? w.plan_id}: margem ${formatBps(w.margin_bps ?? null)}`).join(' · ')} — abaixo do mínimo.
          </AlertDescription>
        </Alert>
      ) : null}

      {current && detail.isLoading ? <Skeleton className="h-48 w-full" /> : null}
      {detail.error ? <ErrorState error={detail.error} onRetry={() => void detail.refetch()} size="compact" /> : null}
      {t ? (
        <section className="flex flex-col gap-4" aria-labelledby="table-title">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h3 id="table-title" className="text-title-card">
              Versão {t.version}: {t.name} <ToneBadge label={TABLE_LABEL[t.status]} tone={TABLE_TONE[t.status]} />
            </h3>
            {canManage ? (
              <div className="flex flex-wrap gap-2">
                <Button variant="secondary" onClick={() => void duplicate(t.id)}>
                  <Copy aria-hidden="true" />
                  Duplicar como nova versão
                </Button>
                {draft ? (
                  <>
                    <Button variant="secondary" onClick={() => setItemEditing('new')}>
                      <Plus aria-hidden="true" />
                      Adicionar item
                    </Button>
                    <Button onClick={() => setActivating(true)}>
                      <Rocket aria-hidden="true" />
                      Ativar esta versão
                    </Button>
                  </>
                ) : null}
              </div>
            ) : null}
          </div>
          {t.status === 'ATIVA' ? (
            <p className="m-0 text-sm text-text-muted">A tabela ativa é imutável. Para mudar preços, duplique como nova versão, edite o rascunho e ative.</p>
          ) : null}
          {t.notes ? <p className="m-0 text-sm">{t.notes}</p> : null}
          <Table>
            <caption className="sr-only">Itens da tabela de preço</caption>
            <TableHeader>
              <TableRow className="h-11 hover:bg-transparent">
                <TableHead scope="col">Tipo</TableHead>
                <TableHead scope="col">Chave</TableHead>
                <TableHead scope="col">Faixa</TableHead>
                <TableHead scope="col">Valor</TableHead>
                <TableHead scope="col">Descrição</TableHead>
                {draft && canManage ? (
                  <TableHead scope="col" className="text-right">
                    Ações
                  </TableHead>
                ) : null}
              </TableRow>
            </TableHeader>
            <TableBody>
              {(t.items ?? []).length === 0 ? (
                <TableRow>
                  <TableCell colSpan={6} className="text-text-muted">
                    Sem itens. Comece pelo preço-base de 30, 60 e 120 segundos.
                  </TableCell>
                </TableRow>
              ) : (
                (t.items ?? []).map((i) => (
                  <TableRow key={i.id}>
                    <TableCell>{PRICE_ITEM_KIND_LABEL[i.kind] ?? i.kind}</TableCell>
                    <TableCell className="font-mono text-xs">{i.key}</TableCell>
                    <TableCell>{i.min_value !== null || i.max_value !== null ? `${i.min_value ?? '—'} a ${i.max_value ?? '—'}` : '—'}</TableCell>
                    <TableCell className="tabular-nums">{itemValueText(i.mode, i.value)}</TableCell>
                    <TableCell>{i.description ?? '—'}</TableCell>
                    {draft && canManage ? (
                      <TableCell className="text-right">
                        <Button variant="ghost" size="icon" aria-label={`Editar item ${i.kind} ${i.key}`} onClick={() => setItemEditing(i)}>
                          <Pencil aria-hidden="true" />
                        </Button>
                        <Button variant="ghost" size="icon" aria-label={`Excluir item ${i.kind} ${i.key}`} onClick={() => setDeleting(i)}>
                          <Trash2 aria-hidden="true" />
                        </Button>
                      </TableCell>
                    ) : null}
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </section>
      ) : null}

      <CreateTableDialog
        open={creating}
        onOpenChange={setCreating}
        onCreated={(n) => {
          setSelectedId(n.id)
          refresh()
        }}
      />
      {t ? (
        <>
          <ItemDialog
            tableId={t.id}
            item={itemEditing}
            onClose={() => setItemEditing(null)}
            onSaved={() => {
              setItemEditing(null)
              refresh()
            }}
          />
          <ConfirmDialog
            open={activating}
            onOpenChange={setActivating}
            title={`Ativar a versão ${t.version}?`}
            description="A versão ativa atual é arquivada e todos os novos preços passam a usar esta tabela. Pedidos já gerados mantêm o preço congelado."
            consequences={['O servidor bloqueia a ativação se algum plano ativo ficar com margem negativa.', 'Depois de ativa, a tabela não pode mais ser editada.']}
            confirmLabel="Ativar versão"
            onConfirm={async () => {
              try {
                const r = await pricingApi.activateTable(t.id)
                setActivationResult(r)
                toast.success(`Versão ${t.version} ativada.`)
                refresh()
              } catch (e) {
                if (isApiError(e) && e.code === 'TABLE_MAKES_PLAN_NEGATIVE') {
                  const plans = (e.details.plans as Array<{ name: string; margin_bps: number }> | undefined) ?? []
                  throw new Error(`${e.message} ${plans.map((p) => `${p.name} (${formatBps(p.margin_bps)})`).join(', ')}`)
                }
                // piso de preço RD (white-label): mínimo em BRL a partir de min_price_cents
                if (isPriceFloorError(e)) throw new Error(describeError(e))
                throw e
              }
            }}
          />
          <ConfirmDialog
            open={!!deleting}
            onOpenChange={(o) => !o && setDeleting(null)}
            destructive
            title="Excluir este item do rascunho?"
            description={deleting ? `${PRICE_ITEM_KIND_LABEL[deleting.kind] ?? deleting.kind} · ${deleting.key} · ${itemValueText(deleting.mode, deleting.value)}` : undefined}
            confirmLabel="Excluir item"
            onConfirm={async () => {
              await pricingApi.deleteItem(t.id, deleting!.id)
              toast.success('Item excluído.')
              refresh()
            }}
          />
        </>
      ) : null}
    </div>
  )
}

function CreateTableDialog({ open, onOpenChange, onCreated }: { open: boolean; onOpenChange: (o: boolean) => void; onCreated: (t: PriceTable) => void }) {
  const [name, setName] = React.useState('')
  const [notes, setNotes] = React.useState('')
  const [error, setError] = React.useState<unknown>(null)
  const [pending, setPending] = React.useState(false)
  React.useEffect(() => {
    if (open) {
      setName('')
      setNotes('')
      setError(null)
    }
  }, [open])
  const save = async () => {
    setPending(true)
    setError(null)
    try {
      const n = await pricingApi.createTable(name.trim(), notes.trim() || null)
      toast.success(`Tabela v${n.version} criada.`)
      onCreated(n)
      onOpenChange(false)
    } catch (e) {
      setError(e)
    } finally {
      setPending(false)
    }
  }
  return (
    <Dialog open={open} onOpenChange={(o) => !pending && onOpenChange(o)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Nova tabela de preço</DialogTitle>
          <DialogDescription>Criada como rascunho vazio. Para partir da tabela atual, use “Duplicar como nova versão”.</DialogDescription>
        </DialogHeader>
        <Field>
          <FieldLabel required>Nome</FieldLabel>
          <Input value={name} onChange={(e) => setName(e.target.value)} maxLength={120} />
        </Field>
        <Field>
          <FieldLabel>Observações</FieldLabel>
          <Textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={3} />
        </Field>
        {error ? (
          <Alert variant="danger">
            <AlertDescription>{describeError(error)}</AlertDescription>
          </Alert>
        ) : null}
        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)} disabled={pending}>
            Cancelar
          </Button>
          <Button onClick={() => void save()} loading={pending} disabled={name.trim().length < 3}>
            Criar tabela
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function ItemDialog({ tableId, item, onClose, onSaved }: { tableId: string; item: PriceItem | 'new' | null; onClose: () => void; onSaved: () => void }) {
  const [kind, setKind] = React.useState('DURATION_BASE')
  const [mode, setMode] = React.useState('BASE_CENTS')
  const [key, setKey] = React.useState('30')
  const [min, setMin] = React.useState('')
  const [max, setMax] = React.useState('')
  const [value, setValue] = React.useState('')
  const [description, setDescription] = React.useState('')
  const [errors, setErrors] = React.useState<Record<string, string>>({})
  const [error, setError] = React.useState<unknown>(null)
  const [pending, setPending] = React.useState(false)
  React.useEffect(() => {
    setErrors({})
    setError(null)
    if (item && item !== 'new') {
      setKind(item.kind)
      setMode(item.mode)
      setKey(item.key)
      setMin(item.min_value === null ? '' : String(item.min_value))
      setMax(item.max_value === null ? '' : String(item.max_value))
      setValue(itemValueToInput(item.mode, item.value))
      setDescription(item.description ?? '')
    } else if (item === 'new') {
      setKind('DURATION_BASE')
      setMode('BASE_CENTS')
      setKey('30')
      setMin('')
      setMax('')
      setValue('')
      setDescription('')
    }
  }, [item])
  if (!item) return null
  const isNew = item === 'new'
  const modes = PRICE_ITEM_MODES[kind] ?? []
  const save = async () => {
    const e: Record<string, string> = {}
    const v = parseItemValue(mode, value)
    if (v === null || v < 0) e.value = 'Valor inválido.'
    const lo = min === '' ? null : Number(min)
    const hi = max === '' ? null : Number(max)
    if ((lo !== null && !Number.isInteger(lo)) || (hi !== null && !Number.isInteger(hi))) e.range = 'Faixa deve ser número inteiro.'
    if (kind === 'TIME_BAND' && (lo === null || hi === null)) e.range = 'Faixa horária exige início e fim em minutos (0–1440).'
    if (!key.trim()) e.key = 'Informe a chave (use * para todas).'
    setErrors(e)
    if (Object.keys(e).length) return
    setPending(true)
    setError(null)
    try {
      if (isNew)
        await pricingApi.addItem(tableId, { kind, mode, key: key.trim(), min_value: lo, max_value: hi, value: v, description: description.trim() || null })
      else
        await pricingApi.patchItem(tableId, (item as PriceItem).id, { key: key.trim(), min_value: lo, max_value: hi, value: v, description: description.trim() || null })
      toast.success('Item salvo.')
      onSaved()
    } catch (err) {
      setError(err)
    } finally {
      setPending(false)
    }
  }
  return (
    <Dialog open onOpenChange={(o) => !o && !pending && onClose()}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>{isNew ? 'Novo item de preço' : 'Editar item'}</DialogTitle>
          <DialogDescription>O servidor valida a combinação tipo × modo × chave.</DialogDescription>
        </DialogHeader>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Field>
            <FieldLabel>Tipo</FieldLabel>
            <Select
              value={kind}
              disabled={!isNew}
              onValueChange={(k) => {
                setKind(k)
                setMode((PRICE_ITEM_MODES[k] ?? [])[0] ?? '')
                setKey(k === 'DURATION_BASE' ? '30' : '*')
              }}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  {Object.keys(PRICE_ITEM_MODES).map((k) => (
                    <SelectItem key={k} value={k}>
                      {PRICE_ITEM_KIND_LABEL[k] ?? k}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
          </Field>
          <Field>
            <FieldLabel>Modo</FieldLabel>
            <Select value={mode} onValueChange={setMode} disabled={!isNew}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  {modes.map((m) => (
                    <SelectItem key={m} value={m}>
                      {PRICE_MODE_LABEL[m] ?? m}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
          </Field>
          <Field invalid={!!errors.key}>
            <FieldLabel required>Chave</FieldLabel>
            <Input value={key} onChange={(e) => setKey(e.target.value)} maxLength={100} />
            {PRICE_ITEM_KEY_HINT[kind] ? <FieldDescription>{PRICE_ITEM_KEY_HINT[kind]}</FieldDescription> : null}
            <FieldError>{errors.key}</FieldError>
          </Field>
          <Field invalid={!!errors.value}>
            <FieldLabel required>{itemValueLabel(mode)}</FieldLabel>
            <Input value={value} onChange={(e) => setValue(e.target.value)} inputMode="decimal" />
            <FieldError>{errors.value}</FieldError>
          </Field>
          {RANGE_KINDS.includes(kind) ? (
            <>
              <Field invalid={!!errors.range}>
                <FieldLabel>{kind === 'TIME_BAND' ? 'Início (minutos desde 00:00)' : 'Mínimo'}</FieldLabel>
                <Input type="number" value={min} onChange={(e) => setMin(e.target.value)} />
                <FieldError>{errors.range}</FieldError>
              </Field>
              <Field>
                <FieldLabel>{kind === 'TIME_BAND' ? 'Fim (minutos, até 1440)' : 'Máximo'}</FieldLabel>
                <Input type="number" value={max} onChange={(e) => setMax(e.target.value)} />
              </Field>
            </>
          ) : null}
        </div>
        <Field>
          <FieldLabel>Descrição</FieldLabel>
          <Input value={description} onChange={(e) => setDescription(e.target.value)} maxLength={255} />
        </Field>
        {error ? (
          <Alert variant="danger">
            <AlertDescription>{describeError(error)}</AlertDescription>
          </Alert>
        ) : null}
        <DialogFooter>
          <Button variant="secondary" onClick={onClose} disabled={pending}>
            Cancelar
          </Button>
          <Button onClick={() => void save()} loading={pending}>
            Salvar item
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
