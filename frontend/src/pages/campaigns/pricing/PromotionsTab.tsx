/** Promoções (desconto % por período, opcionalmente restrito a planos). */
import * as React from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Plus } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Field, FieldError, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState, ErrorState } from '@/components/patterns/states'
import { ToneBadge } from '@/components/campaigns/badges'
import { qk } from '@/lib/queryKeys'
import { formatBps, formatDateTime } from '@/lib/format'
import { pricingApi } from '../lib/api'
import { parsePercentToBps } from '../lib/money'
import { endOfDayUtc, startOfDayUtc, timeZoneLabel } from '@/lib/dates'
import { describeError } from '@/pages/campaigns/lib/errorText'

// Promoção não é dado de estabelecimento: datas valem no fuso padrão de exibição (America/Sao_Paulo).

export function PromotionsTab({ canManage }: { canManage: boolean }) {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: qk.sub('pricing', 'promotions'), queryFn: pricingApi.promotions })
  const [open, setOpen] = React.useState(false)
  const toggle = async (id: string, active: boolean) => {
    try {
      await pricingApi.patchPromotion(id, { active })
      toast.success(active ? 'Promoção ativada.' : 'Promoção desativada.')
      void qc.invalidateQueries({ queryKey: qk.sub('pricing', 'promotions') })
    } catch (e) {
      toast.error(describeError(e))
    }
  }
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-title-section">Promoções</h2>
        {canManage ? (
          <Button onClick={() => setOpen(true)}>
            <Plus aria-hidden="true" />
            Nova promoção
          </Button>
        ) : null}
      </div>
      {q.isLoading ? (
        <Skeleton className="h-24 w-full" />
      ) : q.error ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} size="compact" />
      ) : !q.data?.items.length ? (
        <EmptyState size="compact" headingLevel="h3" title="Nenhuma promoção" description="Promoções aplicam desconto automático no período." />
      ) : (
        <Table>
          <caption className="sr-only">Promoções</caption>
          <TableHeader>
            <TableRow className="h-11 hover:bg-transparent">
              <TableHead scope="col">Nome</TableHead>
              <TableHead scope="col" numeric>
                Desconto
              </TableHead>
              <TableHead scope="col">Validade</TableHead>
              <TableHead scope="col">Situação</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {q.data.items.map((p) => (
              <TableRow key={p.id}>
                <TableCell className="font-semibold">{p.name}</TableCell>
                <TableCell numeric>{formatBps(p.discount_bps, 0)}</TableCell>
                <TableCell>
                  {formatDateTime(p.valid_from)} → {formatDateTime(p.valid_until)}
                </TableCell>
                <TableCell>
                  {canManage ? (
                    <label className="flex min-h-11 items-center gap-2">
                      <Switch checked={p.active} onCheckedChange={(v) => void toggle(p.id, v)} aria-label={`Promoção ${p.name} ativa`} />
                      {p.active ? 'Ativa' : 'Inativa'}
                    </label>
                  ) : (
                    <ToneBadge label={p.active ? 'Ativa' : 'Inativa'} tone={p.active ? 'success' : 'neutral'} />
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      <PromotionDialog open={open} onOpenChange={setOpen} onSaved={() => void qc.invalidateQueries({ queryKey: qk.sub('pricing', 'promotions') })} />
    </div>
  )
}

function PromotionDialog({ open, onOpenChange, onSaved }: { open: boolean; onOpenChange: (o: boolean) => void; onSaved: () => void }) {
  const [name, setName] = React.useState('')
  const [discount, setDiscount] = React.useState('')
  const [from, setFrom] = React.useState('')
  const [until, setUntil] = React.useState('')
  const [errors, setErrors] = React.useState<Record<string, string>>({})
  const [error, setError] = React.useState<unknown>(null)
  const [pending, setPending] = React.useState(false)
  React.useEffect(() => {
    if (open) {
      setName('')
      setDiscount('')
      setFrom('')
      setUntil('')
      setErrors({})
      setError(null)
    }
  }, [open])
  const save = async () => {
    const e: Record<string, string> = {}
    const bps = parsePercentToBps(discount)
    if (name.trim().length < 3) e.name = 'Mínimo de 3 caracteres.'
    if (bps === null || bps < 1 || bps > 10000) e.discount = 'Desconto entre 0,01% e 100%.'
    if (!from || !until || until < from) e.dates = 'Informe o período (fim depois do início).'
    setErrors(e)
    if (Object.keys(e).length) return
    setPending(true)
    setError(null)
    try {
      await pricingApi.createPromotion({ name: name.trim(), discount_bps: bps, valid_from: startOfDayUtc(from), valid_until: endOfDayUtc(until), active: true })
      toast.success('Promoção criada.')
      onSaved()
      onOpenChange(false)
    } catch (err) {
      setError(err)
    } finally {
      setPending(false)
    }
  }
  return (
    <Dialog open={open} onOpenChange={(o) => !pending && onOpenChange(o)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Nova promoção</DialogTitle>
          <DialogDescription>Início às 00:00:00 e fim às 23:59:59 no {timeZoneLabel(null)}.</DialogDescription>
        </DialogHeader>
        <Field invalid={!!errors.name}>
          <FieldLabel required>Nome</FieldLabel>
          <Input value={name} onChange={(e) => setName(e.target.value)} />
          <FieldError>{errors.name}</FieldError>
        </Field>
        <Field invalid={!!errors.discount}>
          <FieldLabel required>Desconto (%)</FieldLabel>
          <Input value={discount} onChange={(e) => setDiscount(e.target.value)} inputMode="decimal" />
          <FieldError>{errors.discount}</FieldError>
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field invalid={!!errors.dates}>
            <FieldLabel required>Início</FieldLabel>
            <Input type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
            <FieldError>{errors.dates}</FieldError>
          </Field>
          <Field>
            <FieldLabel required>Fim</FieldLabel>
            <Input type="date" value={until} onChange={(e) => setUntil(e.target.value)} />
          </Field>
        </div>
        {error ? (
          <Alert variant="danger">
            <AlertDescription>{describeError(error)}</AlertDescription>
          </Alert>
        ) : null}
        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)} disabled={pending}>
            Cancelar
          </Button>
          <Button onClick={() => void save()} loading={pending}>
            Criar promoção
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
