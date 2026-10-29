/**
 * Calculadora de MARGEM (REQ-PLN-009/010/011; RD): simula uma venda com a tabela ativa e
 * mostra TODOS os componentes calculados pelo servidor — gateway, comissão dos
 * estabelecimentos (pool), vendedor, impostos, servidor, equipamento, designer e custos
 * diretos. Margem negativa = bloqueio; abaixo do mínimo (40%) = alerta.
 * Inclui o piso de preço RD do white-label (validação do servidor).
 */
import * as React from 'react'
import { useQuery } from '@tanstack/react-query'
import { Calculator, ShieldCheck } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Field, FieldError, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Skeleton } from '@/components/ui/skeleton'
import { ErrorState } from '@/components/patterns/states'
import { QuoteBreakdown } from '@/components/campaigns/quote-breakdown'
import { qk } from '@/lib/queryKeys'
import { formatCents, formatDateTime } from '@/lib/format'
import { pricingApi } from './lib/api'
import { FREQ_LABEL, PERIOD_LABEL } from './lib/labels'
import { ALLOWED_DURATIONS } from './lib/rules'
import { parseMoneyToCents, parsePercentToBps } from './lib/money'
import type { FreqKind, PeriodType, Quote } from './lib/types'
import { describeError } from '@/pages/campaigns/lib/errorText'

export default function MarginPage() {
  const [f, setF] = React.useState({
    duration_s: 30,
    freq_kind: 'PER_HOUR' as FreqKind,
    freq_value: '4',
    period_type: 'MENSAL' as PeriodType,
    period_days: '30',
    screens: '10',
    discount: '0',
    priority: 'STANDARD',
    exclusivity: 'NONE',
    art: false,
  })
  const [errors, setErrors] = React.useState<Record<string, string>>({})
  const [result, setResult] = React.useState<Quote | null>(null)
  const [error, setError] = React.useState<unknown>(null)
  const [pending, setPending] = React.useState(false)
  const set = (p: Partial<typeof f>) => setF((x) => ({ ...x, ...p }))

  const run = async (e: React.FormEvent) => {
    e.preventDefault()
    const errs: Record<string, string> = {}
    const screens = Math.floor(Number(f.screens))
    const freq = Math.floor(Number(f.freq_value))
    const disc = parsePercentToBps(f.discount)
    if (!(screens >= 1 && screens <= 10000)) errs.screens = 'De 1 a 10.000 telas.'
    if (!(freq >= 1 && freq <= 1440)) errs.freq = 'De 1 a 1440 inserções.'
    if (disc === null || disc < 0 || disc > 10000) errs.discount = 'Desconto entre 0 e 100%.'
    if (f.period_type === 'AVULSO' && !(Number(f.period_days) >= 1)) errs.days = 'Informe os dias.'
    setErrors(errs)
    if (Object.keys(errs).length) return
    setPending(true)
    setError(null)
    try {
      const r = await pricingApi.marginSimulator({
        duration_s: f.duration_s,
        freq_kind: f.freq_kind,
        freq_value: freq,
        period_type: f.period_type,
        period_days: f.period_type === 'AVULSO' ? Math.floor(Number(f.period_days)) : null,
        screens,
        discount_bps: disc,
        priority: f.priority,
        exclusivity: f.exclusivity,
        request_art_service: f.art,
      })
      setResult({ ...r.quote, margin: r.margin ?? r.quote.margin })
    } catch (err) {
      setResult(null)
      setError(err)
    } finally {
      setPending(false)
    }
  }

  return (
    <>
      <PageHeader title="Margem" description="Simulação com a tabela de preços ativa. Mínimo de 40%; margem negativa bloqueia a venda." />
      <div className="grid grid-cols-1 gap-6 xl:grid-cols-12">
        <Card className="xl:col-span-5">
          <CardTitle>Simular venda</CardTitle>
          <form onSubmit={run} className="flex flex-col gap-4" noValidate>
            <div className="flex flex-col gap-2">
              <span id="sim-duration" className="text-sm font-semibold">
                Duração do anúncio
              </span>
              <ToggleGroup type="single" value={String(f.duration_s)} onValueChange={(v) => v && set({ duration_s: Number(v) })} aria-labelledby="sim-duration">
                {ALLOWED_DURATIONS.map((d) => (
                  <ToggleGroupItem key={d} value={String(d)}>
                    {d} s
                  </ToggleGroupItem>
                ))}
              </ToggleGroup>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <Field>
                <FieldLabel>Frequência</FieldLabel>
                <Select value={f.freq_kind} onValueChange={(v) => set({ freq_kind: v as FreqKind })}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      <SelectItem value="PER_HOUR">{FREQ_LABEL.PER_HOUR}</SelectItem>
                      <SelectItem value="PER_DAY">{FREQ_LABEL.PER_DAY}</SelectItem>
                    </SelectGroup>
                  </SelectContent>
                </Select>
              </Field>
              <Field invalid={!!errors.freq}>
                <FieldLabel>Inserções</FieldLabel>
                <Input type="number" value={f.freq_value} onChange={(e) => set({ freq_value: e.target.value })} />
                <FieldError>{errors.freq}</FieldError>
              </Field>
              <Field>
                <FieldLabel>Período</FieldLabel>
                <Select value={f.period_type} onValueChange={(v) => set({ period_type: v as PeriodType })}>
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
              </Field>
              {f.period_type === 'AVULSO' ? (
                <Field invalid={!!errors.days}>
                  <FieldLabel>Dias</FieldLabel>
                  <Input type="number" value={f.period_days} onChange={(e) => set({ period_days: e.target.value })} />
                  <FieldError>{errors.days}</FieldError>
                </Field>
              ) : (
                <div />
              )}
              <Field invalid={!!errors.screens}>
                <FieldLabel>Telas</FieldLabel>
                <Input type="number" value={f.screens} onChange={(e) => set({ screens: e.target.value })} />
                <FieldError>{errors.screens}</FieldError>
              </Field>
              <Field invalid={!!errors.discount}>
                <FieldLabel>Desconto (%)</FieldLabel>
                <Input value={f.discount} onChange={(e) => set({ discount: e.target.value })} inputMode="decimal" />
                <FieldError>{errors.discount}</FieldError>
              </Field>
              <Field>
                <FieldLabel>Prioridade</FieldLabel>
                <Select value={f.priority} onValueChange={(v) => set({ priority: v })}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      <SelectItem value="STANDARD">Padrão</SelectItem>
                      <SelectItem value="PREMIUM">Premium</SelectItem>
                    </SelectGroup>
                  </SelectContent>
                </Select>
              </Field>
              <Field>
                <FieldLabel>Exclusividade</FieldLabel>
                <Select value={f.exclusivity} onValueChange={(v) => set({ exclusivity: v })}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      <SelectItem value="NONE">Sem</SelectItem>
                      <SelectItem value="COMPETITOR_EXCLUSIVE">De concorrente</SelectItem>
                    </SelectGroup>
                  </SelectContent>
                </Select>
              </Field>
            </div>
            <Field orientation="horizontal" className="min-h-11">
              <Switch checked={f.art} onCheckedChange={(v) => set({ art: v })} />
              <FieldLabel className="font-normal">Incluir criação de arte (custo de designer)</FieldLabel>
            </Field>
            <Button type="submit" loading={pending}>
              <Calculator aria-hidden="true" />
              Calcular margem
            </Button>
          </form>
        </Card>
        <Card className="xl:col-span-7">
          <CardTitle>Resultado (servidor)</CardTitle>
          {error ? (
            <Alert variant="danger">
              <AlertDescription>{describeError(error)}</AlertDescription>
            </Alert>
          ) : null}
          {result ? <QuoteBreakdown quote={result} showGroups={false} /> : !error ? <p className="m-0 text-sm text-text-muted">Preencha a simulação e calcule.</p> : null}
        </Card>
        <FloorsCard />
      </div>
    </>
  )
}

function FloorsCard() {
  const q = useQuery({ queryKey: qk.sub('pricing', 'floors'), queryFn: pricingApi.floors, retry: false })
  const [key, setKey] = React.useState('')
  const [price, setPrice] = React.useState('')
  const [check, setCheck] = React.useState<{ ok: boolean; text: string } | null>(null)
  const [pending, setPending] = React.useState(false)
  const run = async (e: React.FormEvent) => {
    e.preventDefault()
    const cents = parseMoneyToCents(price)
    if (!key || cents === null) {
      setCheck({ ok: false, text: 'Escolha o item e informe o preço.' })
      return
    }
    setPending(true)
    try {
      const r = await pricingApi.checkFloor(key, cents)
      setCheck({ ok: true, text: `Preço aceito${r.min_price_cents !== null ? ` (piso ${formatCents(r.min_price_cents)})` : ' (sem piso cadastrado)'}.` })
    } catch (err) {
      setCheck({ ok: false, text: describeError(err) })
    } finally {
      setPending(false)
    }
  }
  return (
    <Card className="xl:col-span-12">
      <CardTitle>Piso de preço RD (white-label)</CardTitle>
      <CardDescription>Instâncias white-label não podem vender abaixo do piso da RD. Só os Admin Master alteram o piso.</CardDescription>
      {q.isLoading ? (
        <Skeleton className="h-24 w-full" />
      ) : q.error ? (
        <ErrorState error={q.error} size="compact" onRetry={() => void q.refetch()} />
      ) : !q.data?.items.length ? (
        <p className="m-0 text-sm text-text-muted">Nenhum piso cadastrado.</p>
      ) : (
        <Table>
          <caption className="sr-only">Pisos de preço</caption>
          <TableHeader>
            <TableRow className="h-11 hover:bg-transparent">
              <TableHead scope="col">Item</TableHead>
              <TableHead scope="col">Unidade</TableHead>
              <TableHead scope="col" numeric>
                Preço mínimo
              </TableHead>
              <TableHead scope="col">Atualizado</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {q.data.items.map((fl) => (
              <TableRow key={fl.key}>
                <TableCell>
                  {fl.label} <span className="block font-mono text-xs text-text-muted">{fl.key}</span>
                </TableCell>
                <TableCell>{fl.unit}</TableCell>
                <TableCell numeric className="tabular-nums">
                  {formatCents(fl.min_price_cents)}
                </TableCell>
                <TableCell>{formatDateTime(fl.updated_at)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      <form onSubmit={run} className="flex flex-wrap items-end gap-3">
        <Field className="w-64">
          <FieldLabel>Item</FieldLabel>
          <Select value={key} onValueChange={setKey}>
            <SelectTrigger>
              <SelectValue placeholder="Selecione" />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                {(q.data?.items ?? []).map((fl) => (
                  <SelectItem key={fl.key} value={fl.key}>
                    {fl.label}
                  </SelectItem>
                ))}
              </SelectGroup>
            </SelectContent>
          </Select>
        </Field>
        <Field className="w-48">
          <FieldLabel>Preço da instância (R$)</FieldLabel>
          <Input value={price} onChange={(e) => setPrice(e.target.value)} inputMode="decimal" />
        </Field>
        <Button type="submit" variant="secondary" loading={pending} disabled={!q.data?.items.length}>
          <ShieldCheck aria-hidden="true" />
          Validar no servidor
        </Button>
      </form>
      {check ? (
        <p role="status" className={check.ok ? 'm-0 text-sm text-success' : 'm-0 text-sm text-danger'}>
          {check.text}
        </p>
      ) : null}
    </Card>
  )
}
