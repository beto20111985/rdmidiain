/**
 * Audiência ESTIMADA (nunca medida; exibição ≠ audiência — P002 #28, DS §12): valor atual com
 * método, histórico append-only e registro de nova estimativa (manual ou por fluxo × visibilidade).
 * Usado no detalhe do estabelecimento e da tela.
 */
import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Users } from 'lucide-react'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState, ErrorState } from '@/components/patterns/states'
import { errorMessage } from '@/lib/errors'
import { formatDateTime, formatNumber } from '@/lib/format'
import type { QueryDomain } from '@/lib/queryKeys'
import type { Paginated } from '@/lib/http'
import { AUDIENCE_METHOD_LABEL, labelOf } from '@/pages/ops/api/labels'
import type { AudienceEstimate, AudienceLabel } from '@/pages/ops/api/types'

export function AudiencePanel({
  current,
  domain,
  subjectId,
  list,
  add,
  canEdit,
  timeZone,
}: {
  current: AudienceLabel | null
  domain: QueryDomain
  subjectId: string
  list: () => Promise<Paginated<AudienceEstimate>>
  add: (body: Record<string, unknown>) => Promise<unknown>
  canEdit: boolean
  timeZone?: string
}) {
  const qc = useQueryClient()
  const key = [domain, 'audience', subjectId]
  const hist = useQuery({ queryKey: key, queryFn: list })
  const [method, setMethod] = React.useState<'MANUAL' | 'FLOW_ESTIMATE'>('MANUAL')
  const [people, setPeople] = React.useState('')
  const [visitors, setVisitors] = React.useState('')
  const [visibility, setVisibility] = React.useState('')
  const [note, setNote] = React.useState('')
  const [touched, setTouched] = React.useState(false)

  const nPeople = Number(people)
  const nVisitors = Number(visitors)
  const nVis = Number(visibility.replace(',', '.'))
  const peopleOk = people.trim() !== '' && Number.isInteger(nPeople) && nPeople >= 0 && nPeople <= 10_000_000
  const visitorsOk = visitors.trim() !== '' && Number.isInteger(nVisitors) && nVisitors >= 0 && nVisitors <= 10_000_000
  const visOk = visibility.trim() !== '' && Number.isFinite(nVis) && nVis > 0 && nVis <= 100
  const valid = method === 'MANUAL' ? peopleOk : visitorsOk && visOk
  const preview = method === 'FLOW_ESTIMATE' && visitorsOk && visOk ? Math.floor((nVisitors * Math.round(nVis * 100)) / 10000) : null

  const m = useMutation({
    mutationFn: () =>
      add(
        method === 'MANUAL'
          ? { method, daily_people: nPeople, note: note.trim() || null }
          : { method, daily_visitors: nVisitors, visibility_bps: Math.round(nVis * 100), note: note.trim() || null },
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: [domain] })
      toast.success('Estimativa de audiência registrada.')
      setPeople('')
      setVisitors('')
      setVisibility('')
      setNote('')
      setTouched(false)
    },
  })

  return (
    <div className="grid grid-cols-1 gap-6 xl:grid-cols-12">
      <Card className="xl:col-span-5">
        <CardHeader>
          <CardTitle>Audiência atual</CardTitle>
          <Badge tone="warning">ESTIMADA</Badge>
        </CardHeader>
        {current ? (
          <div className="flex flex-col gap-1">
            <p className="m-0 text-kpi-secondary tabular-nums text-text">
              {formatNumber(current.value)} <span className="text-sm font-normal text-text-muted">pessoas/dia (estimativa)</span>
            </p>
            <p className="m-0 text-sm text-text-muted">Método: {labelOf(AUDIENCE_METHOD_LABEL, current.method)}</p>
          </div>
        ) : (
          <p className="m-0 text-sm text-text-muted">Sem estimativa registrada.</p>
        )}
        <CardDescription>
          Audiência é uma estimativa com método declarado — não é medição de pessoas impactadas e não deriva das exibições.
        </CardDescription>
      </Card>

      {canEdit ? (
        <Card className="xl:col-span-7">
          <CardHeader>
            <CardTitle>Registrar nova estimativa</CardTitle>
          </CardHeader>
          <form
            noValidate
            className="flex flex-col gap-4"
            onSubmit={(e) => {
              e.preventDefault()
              setTouched(true)
              if (valid) m.mutate()
            }}
          >
            <div className="flex flex-col gap-2">
              <span id={`aud-method-${subjectId}`} className="text-sm font-semibold text-text">
                Método
              </span>
              <ToggleGroup
                type="single"
                value={method}
                onValueChange={(v) => v && setMethod(v as typeof method)}
                aria-labelledby={`aud-method-${subjectId}`}
              >
                <ToggleGroupItem value="MANUAL">Informada manualmente</ToggleGroupItem>
                <ToggleGroupItem value="FLOW_ESTIMATE">Fluxo × visibilidade</ToggleGroupItem>
              </ToggleGroup>
            </div>
            {method === 'MANUAL' ? (
              <Field invalid={touched && !peopleOk} required>
                <FieldLabel>Pessoas por dia (estimativa)</FieldLabel>
                <Input value={people} onChange={(e) => setPeople(e.target.value.replace(/\D/g, ''))} inputMode="numeric" />
                <FieldError>{touched && !peopleOk ? 'Informe um número inteiro de pessoas/dia.' : null}</FieldError>
              </Field>
            ) : (
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <Field invalid={touched && !visitorsOk} required>
                  <FieldLabel>Fluxo diário de visitantes</FieldLabel>
                  <Input value={visitors} onChange={(e) => setVisitors(e.target.value.replace(/\D/g, ''))} inputMode="numeric" />
                  <FieldError>{touched && !visitorsOk ? 'Informe o fluxo diário (número inteiro).' : null}</FieldError>
                </Field>
                <Field invalid={touched && !visOk} required>
                  <FieldLabel>Visibilidade da tela (%)</FieldLabel>
                  <Input value={visibility} onChange={(e) => setVisibility(e.target.value)} inputMode="decimal" />
                  <FieldDescription>Parcela dos visitantes que vê a tela (0,01 a 100%).</FieldDescription>
                  <FieldError>{touched && !visOk ? 'Entre 0,01 e 100.' : null}</FieldError>
                </Field>
                <p className="m-0 text-sm text-text-muted sm:col-span-2" aria-live="polite">
                  {preview !== null ? `Resultado estimado: ${formatNumber(preview)} pessoas/dia (fluxo × visibilidade).` : ''}
                </p>
              </div>
            )}
            <Field>
              <FieldLabel>Fonte/observação</FieldLabel>
              <Textarea value={note} onChange={(e) => setNote(e.target.value)} rows={2} maxLength={1000} />
            </Field>
            {m.error ? (
              <Alert variant="danger">
                <AlertDescription className="text-text">{errorMessage(m.error)}</AlertDescription>
              </Alert>
            ) : null}
            <Button type="submit" className="w-fit" loading={m.isPending}>
              Registrar estimativa
            </Button>
          </form>
        </Card>
      ) : null}

      <Card className="xl:col-span-12">
        <CardHeader>
          <CardTitle>Histórico de estimativas</CardTitle>
        </CardHeader>
        {hist.isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : hist.error ? (
          <ErrorState error={hist.error} onRetry={() => void hist.refetch()} size="compact" />
        ) : !hist.data?.items.length ? (
          <EmptyState icon={Users} size="compact" headingLevel="h3" title="Nenhuma estimativa registrada." />
        ) : (
          <ol className="m-0 flex list-none flex-col divide-y divide-border p-0">
            {hist.data.items.map((a) => (
              <li key={a.id} className="flex flex-wrap items-baseline justify-between gap-2 py-3">
                <span className="tabular-nums text-text">
                  {formatNumber(a.daily_people)} pessoas/dia <span className="text-xs text-text-muted">(estimada)</span>
                </span>
                <span className="text-xs text-text-muted">
                  {labelOf(AUDIENCE_METHOD_LABEL, a.method)} · {formatDateTime(a.created_at, timeZone)}
                  {a.note ? ` · ${a.note}` : ''}
                </span>
              </li>
            ))}
          </ol>
        )}
      </Card>
    </div>
  )
}
