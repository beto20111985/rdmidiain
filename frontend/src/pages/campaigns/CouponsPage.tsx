/**
 * Cupons (PG-16; RD): desconto percentual ou de valor fixo, validade, limite total e por
 * cliente, pedido mínimo e uso (contagem do servidor). Uso concorrente é resolvido no servidor.
 */
import * as React from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Plus } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState, ErrorState } from '@/components/patterns/states'
import { ToneBadge } from '@/components/campaigns/badges'
import { Can } from '@/components/auth/guards'
import { useCan } from '@/hooks/usePermissions'
import { qk } from '@/lib/queryKeys'
import { formatBps, formatCents, formatDateTime, formatNumber } from '@/lib/format'
import { pricingApi } from './lib/api'
import { parseMoneyToCents, parsePercentToBps } from './lib/money'
import { endOfDayUtc, startOfDayUtc, timeZoneLabel, utcToZonedDate } from '@/lib/dates'
import type { Coupon } from './lib/types'
import { describeError } from '@/pages/campaigns/lib/errorText'

export default function CouponsPage() {
  const can = useCan()
  const qc = useQueryClient()
  const q = useQuery({ queryKey: qk.sub('pricing', 'coupons'), queryFn: pricingApi.coupons })
  const [open, setOpen] = React.useState(false)
  const [editing, setEditing] = React.useState<Coupon | null>(null)
  const manage = can('pricing.manage')
  const refresh = () => void qc.invalidateQueries({ queryKey: qk.sub('pricing', 'coupons') })
  const now = Date.now()

  return (
    <>
      <PageHeader
        title="Cupons"
        description={q.data ? `${formatNumber(q.data.items.length)} cupom(ns)` : undefined}
        actions={
          <Can permission="pricing.manage">
            <Button onClick={() => setOpen(true)}>
              <Plus aria-hidden="true" />
              Novo cupom
            </Button>
          </Can>
        }
      />
      <Card>
        {q.isLoading ? (
          <Skeleton className="h-40 w-full" />
        ) : q.error ? (
          <ErrorState error={q.error} onRetry={() => void q.refetch()} size="compact" />
        ) : !q.data?.items.length ? (
          <EmptyState title="Nenhum cupom" description="Cupons dão desconto no checkout do anunciante." size="compact" headingLevel="h2" />
        ) : (
          <Table>
            <caption className="sr-only">Cupons</caption>
            <TableHeader>
              <TableRow className="h-11 hover:bg-transparent">
                <TableHead scope="col">Código</TableHead>
                <TableHead scope="col">Desconto</TableHead>
                <TableHead scope="col">Validade</TableHead>
                <TableHead scope="col" numeric>
                  Uso
                </TableHead>
                <TableHead scope="col" numeric>
                  Pedido mínimo
                </TableHead>
                <TableHead scope="col">Situação</TableHead>
                {manage ? (
                  <TableHead scope="col" className="text-right">
                    Ações
                  </TableHead>
                ) : null}
              </TableRow>
            </TableHeader>
            <TableBody>
              {q.data.items.map((c) => {
                const expired = new Date(c.valid_until).getTime() < now
                const exhausted = c.max_uses !== null && c.uses_count >= c.max_uses
                return (
                  <TableRow key={c.id}>
                    <TableCell>
                      <span className="font-mono font-semibold">{c.code}</span>
                      {c.description ? <span className="block text-xs text-text-muted">{c.description}</span> : null}
                    </TableCell>
                    <TableCell>{c.discount_type === 'PERCENT' ? formatBps(c.discount_bps, 0) : formatCents(c.discount_cents)}</TableCell>
                    <TableCell>
                      {formatDateTime(c.valid_from)} → {formatDateTime(c.valid_until)}
                    </TableCell>
                    <TableCell numeric className="tabular-nums">
                      {formatNumber(c.uses_count)} / {c.max_uses === null ? '∞' : formatNumber(c.max_uses)}
                      {c.max_uses_per_org ? <span className="block text-xs text-text-muted">{c.max_uses_per_org} por cliente</span> : null}
                    </TableCell>
                    <TableCell numeric className="tabular-nums">
                      {c.min_order_cents ? formatCents(c.min_order_cents) : '—'}
                    </TableCell>
                    <TableCell>
                      {!c.active ? (
                        <ToneBadge label="Inativo" tone="neutral" />
                      ) : expired ? (
                        <ToneBadge label="Expirado" tone="neutral" />
                      ) : exhausted ? (
                        <ToneBadge label="Esgotado" tone="warning" />
                      ) : (
                        <ToneBadge label="Ativo" tone="success" />
                      )}
                    </TableCell>
                    {manage ? (
                      <TableCell className="text-right">
                        <Button variant="ghost" onClick={() => setEditing(c)}>
                          Editar
                        </Button>
                      </TableCell>
                    ) : null}
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        )}
      </Card>
      <CouponDialog open={open} onOpenChange={setOpen} onSaved={refresh} />
      <EditCouponDialog coupon={editing} onClose={() => setEditing(null)} onSaved={refresh} />
    </>
  )
}

/** Cupom não é dado de estabelecimento: datas valem no fuso padrão de exibição (America/Sao_Paulo). */
function day(d: string, end = false) {
  return end ? endOfDayUtc(d) : startOfDayUtc(d)
}

function CouponDialog({ open, onOpenChange, onSaved }: { open: boolean; onOpenChange: (o: boolean) => void; onSaved: () => void }) {
  const [f, setF] = React.useState({ code: '', description: '', type: 'PERCENT' as 'PERCENT' | 'FIXED', amount: '', from: '', until: '', maxUses: '', perOrg: '', minOrder: '' })
  const [errors, setErrors] = React.useState<Record<string, string>>({})
  const [error, setError] = React.useState<unknown>(null)
  const [pending, setPending] = React.useState(false)
  React.useEffect(() => {
    if (open) {
      setF({ code: '', description: '', type: 'PERCENT', amount: '', from: '', until: '', maxUses: '', perOrg: '', minOrder: '' })
      setErrors({})
      setError(null)
    }
  }, [open])
  const set = (p: Partial<typeof f>) => setF((x) => ({ ...x, ...p }))
  const save = async () => {
    const e: Record<string, string> = {}
    if (!/^[A-Za-z0-9_-]{3,40}$/.test(f.code.trim())) e.code = 'De 3 a 40 letras, números, "-" ou "_".'
    const bps = f.type === 'PERCENT' ? parsePercentToBps(f.amount) : 0
    const cents = f.type === 'FIXED' ? parseMoneyToCents(f.amount) : 0
    if (f.type === 'PERCENT' && (bps === null || bps < 1 || bps > 10000)) e.amount = 'Percentual entre 0,01% e 100%.'
    if (f.type === 'FIXED' && (cents === null || cents <= 0)) e.amount = 'Informe um valor maior que zero.'
    if (!f.from || !f.until || f.until < f.from) e.dates = 'Informe a validade (fim depois do início).'
    const minOrder = f.minOrder.trim() ? parseMoneyToCents(f.minOrder) : 0
    if (minOrder === null || minOrder < 0) e.minOrder = 'Valor inválido.'
    setErrors(e)
    if (Object.keys(e).length) return
    setPending(true)
    setError(null)
    try {
      await pricingApi.createCoupon({
        code: f.code.trim().toUpperCase(),
        description: f.description.trim() || null,
        discount_type: f.type,
        discount_bps: f.type === 'PERCENT' ? bps : 0,
        discount_cents: f.type === 'FIXED' ? cents : 0,
        valid_from: day(f.from),
        valid_until: day(f.until, true),
        max_uses: f.maxUses.trim() ? Math.floor(Number(f.maxUses)) : null,
        max_uses_per_org: f.perOrg.trim() ? Math.floor(Number(f.perOrg)) : null,
        min_order_cents: minOrder ?? 0,
        active: true,
      })
      toast.success('Cupom criado.')
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
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>Novo cupom</DialogTitle>
          <DialogDescription>Validade de 00:00:00 a 23:59:59 no {timeZoneLabel(null)}. O código é salvo em maiúsculas.</DialogDescription>
        </DialogHeader>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Field invalid={!!errors.code}>
            <FieldLabel required>Código</FieldLabel>
            <Input value={f.code} onChange={(e) => set({ code: e.target.value })} autoComplete="off" />
            <FieldError>{errors.code}</FieldError>
          </Field>
          <Field>
            <FieldLabel>Descrição</FieldLabel>
            <Input value={f.description} onChange={(e) => set({ description: e.target.value })} maxLength={255} />
          </Field>
          <div className="flex flex-col gap-2">
            <span id="coupon-type" className="text-sm font-semibold">
              Tipo de desconto
            </span>
            <ToggleGroup type="single" value={f.type} onValueChange={(v) => v && set({ type: v as 'PERCENT' | 'FIXED', amount: '' })} aria-labelledby="coupon-type">
              <ToggleGroupItem value="PERCENT">Percentual</ToggleGroupItem>
              <ToggleGroupItem value="FIXED">Valor fixo</ToggleGroupItem>
            </ToggleGroup>
          </div>
          <Field invalid={!!errors.amount}>
            <FieldLabel required>{f.type === 'PERCENT' ? 'Desconto (%)' : 'Desconto (R$)'}</FieldLabel>
            <Input value={f.amount} onChange={(e) => set({ amount: e.target.value })} inputMode="decimal" />
            {f.type === 'FIXED' ? <FieldDescription>Em pedidos com várias campanhas, o servidor rateia o valor.</FieldDescription> : null}
            <FieldError>{errors.amount}</FieldError>
          </Field>
          <Field invalid={!!errors.dates}>
            <FieldLabel required>Válido de</FieldLabel>
            <Input type="date" value={f.from} onChange={(e) => set({ from: e.target.value })} />
            <FieldError>{errors.dates}</FieldError>
          </Field>
          <Field>
            <FieldLabel required>Válido até</FieldLabel>
            <Input type="date" value={f.until} onChange={(e) => set({ until: e.target.value })} />
          </Field>
          <Field>
            <FieldLabel>Limite total de usos</FieldLabel>
            <Input type="number" min={1} value={f.maxUses} onChange={(e) => set({ maxUses: e.target.value })} />
            <FieldDescription>Vazio = sem limite.</FieldDescription>
          </Field>
          <Field>
            <FieldLabel>Limite por cliente</FieldLabel>
            <Input type="number" min={1} value={f.perOrg} onChange={(e) => set({ perOrg: e.target.value })} />
          </Field>
          <Field invalid={!!errors.minOrder}>
            <FieldLabel>Pedido mínimo (R$)</FieldLabel>
            <Input value={f.minOrder} onChange={(e) => set({ minOrder: e.target.value })} inputMode="decimal" />
            <FieldError>{errors.minOrder}</FieldError>
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
            Criar cupom
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function EditCouponDialog({ coupon, onClose, onSaved }: { coupon: Coupon | null; onClose: () => void; onSaved: () => void }) {
  const [active, setActive] = React.useState(true)
  const [until, setUntil] = React.useState('')
  const [maxUses, setMaxUses] = React.useState('')
  const [description, setDescription] = React.useState('')
  const [error, setError] = React.useState<unknown>(null)
  const [pending, setPending] = React.useState(false)
  React.useEffect(() => {
    if (coupon) {
      setActive(coupon.active)
      setUntil(utcToZonedDate(coupon.valid_until))
      setMaxUses(coupon.max_uses === null ? '' : String(coupon.max_uses))
      setDescription(coupon.description ?? '')
      setError(null)
    }
  }, [coupon])
  if (!coupon) return null
  const save = async () => {
    setPending(true)
    setError(null)
    try {
      await pricingApi.patchCoupon(coupon.id, {
        active,
        valid_until: day(until, true),
        ...(maxUses.trim() ? { max_uses: Math.floor(Number(maxUses)) } : {}),
        description: description.trim() || null,
      })
      toast.success('Cupom atualizado.')
      onSaved()
      onClose()
    } catch (e) {
      setError(e)
    } finally {
      setPending(false)
    }
  }
  return (
    <Dialog open onOpenChange={(o) => !o && !pending && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Editar cupom {coupon.code}</DialogTitle>
          <DialogDescription>Código, tipo e valor do desconto não mudam depois de criados.</DialogDescription>
        </DialogHeader>
        <Field orientation="horizontal" className="min-h-11">
          <Switch checked={active} onCheckedChange={setActive} />
          <FieldLabel className="font-normal">Ativo</FieldLabel>
        </Field>
        <Field>
          <FieldLabel>Válido até</FieldLabel>
          <Input type="date" value={until} onChange={(e) => setUntil(e.target.value)} />
        </Field>
        <Field>
          <FieldLabel>Limite total de usos</FieldLabel>
          <Input type="number" min={coupon.uses_count || 1} value={maxUses} onChange={(e) => setMaxUses(e.target.value)} />
          <FieldDescription>Não pode ser menor que os {coupon.uses_count} usos já feitos.</FieldDescription>
        </Field>
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
            Salvar
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
