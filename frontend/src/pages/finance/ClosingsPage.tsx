/**
 * Financeiro → Fechamento mensal (P001 §91): status por mês, snapshot (balancete + DRE) com
 * SHA-256; fechar é IRREVERSÍVEL (o banco passa a recusar lançamentos no período). A
 * competência de comissões precisa estar fechada antes.
 */
import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Lock, LockKeyhole } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Sheet, SheetBody, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Skeleton } from '@/components/ui/skeleton'
import { ConfirmDialog } from '@/components/patterns/confirm-dialog'
import { EmptyState, ErrorState } from '@/components/patterns/states'
import { DetailList } from '@/components/finance/detail-list'
import { ToneBadge } from '@/components/finance/status-pill'
import { formatIsoDate, formatIsoMonth } from '@/components/finance/period'
import { todayIn } from '@/lib/dates'
import { useUserNames } from '@/components/finance/hooks'
import { Can } from '@/components/auth/guards'
import { qk } from '@/lib/queryKeys'
import { errorMessage, isApiError } from '@/lib/errors'
import { formatCents, formatDateTime, formatNumber } from '@/lib/format'
import { financeApi, type Closing, type DreLine } from './api'

function previousMonth(): string {
  const t = todayIn()
  const d = new Date(`${t.slice(0, 7)}-01T00:00:00Z`)
  d.setUTCMonth(d.getUTCMonth() - 1)
  return d.toISOString().slice(0, 7)
}

function ClosingSheet({ id, onOpenChange }: { id: string | null; onOpenChange: (o: boolean) => void }) {
  const q = useQuery({ queryKey: qk.sub('finance', 'closings', 'detail', id ?? ''), queryFn: () => financeApi.closing(id as string), enabled: !!id })
  const c = q.data
  const snap = (c?.snapshot ?? {}) as { dre?: { lines?: DreLine[]; result_cents?: number }; trial_balance?: unknown[] }
  return (
    <Sheet open={!!id} onOpenChange={onOpenChange}>
      <SheetContent>
        <SheetHeader>
          <SheetTitle>Fechamento {c ? formatIsoMonth(c.period_start) : ''}</SheetTitle>
          <SheetDescription>Snapshot imutável do período (balancete + DRE) e hash SHA-256.</SheetDescription>
        </SheetHeader>
        <SheetBody className="flex flex-col gap-4 pb-6">
          {q.isLoading ? (
            <Skeleton className="h-40 w-full" />
          ) : q.error ? (
            <ErrorState error={q.error} onRetry={() => void q.refetch()} size="compact" />
          ) : c ? (
            <>
              <DetailList
                columns={1}
                items={[
                  { label: 'Período', value: `${formatIsoDate(c.period_start)} a ${formatIsoDate(c.period_end)}` },
                  { label: 'Fechado em', value: formatDateTime(c.closed_at) },
                  { label: 'Lançamentos no período', value: formatNumber(c.transaction_count), numeric: true },
                  { label: 'Contas no balancete', value: formatNumber(snap.trial_balance?.length ?? null), numeric: true },
                  { label: 'SHA-256 do snapshot', value: <span className="break-all font-mono text-xs">{c.snapshot_sha256}</span> },
                ]}
              />
              {snap.dre?.lines?.length ? (
                <Table>
                  <caption className="text-left text-sm font-semibold text-text">DRE do mês</caption>
                  <TableHeader>
                    <TableRow className="h-11 hover:bg-transparent">
                      <TableHead scope="col">Linha</TableHead>
                      <TableHead scope="col" numeric>
                        Valor
                      </TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {snap.dre.lines.map((l) => (
                      <TableRow key={l.group}>
                        <TableCell>{l.label}</TableCell>
                        <TableCell numeric>{formatCents(l.amount_cents)}</TableCell>
                      </TableRow>
                    ))}
                    <TableRow>
                      <TableCell className="font-semibold">Resultado</TableCell>
                      <TableCell numeric className="font-semibold">
                        {formatCents(snap.dre.result_cents ?? null)}
                      </TableCell>
                    </TableRow>
                  </TableBody>
                </Table>
              ) : null}
            </>
          ) : null}
        </SheetBody>
      </SheetContent>
    </Sheet>
  )
}

export default function ClosingsPage() {
  const qc = useQueryClient()
  const nameOf = useUserNames()
  const q = useQuery({ queryKey: qk.sub('finance', 'closings', 'list'), queryFn: financeApi.closings })
  const [month, setMonth] = React.useState(previousMonth())
  const [monthError, setMonthError] = React.useState<string | null>(null)
  const [confirm, setConfirm] = React.useState(false)
  const [openId, setOpenId] = React.useState<string | null>(null)
  const [hint, setHint] = React.useState<string | null>(null)
  const closeDue = useMutation({
    mutationFn: financeApi.closeDueCompetences,
    onSuccess: async (r) => {
      toast.success(r.closed.length ? `${r.closed.length} competência(s) de comissão fechada(s).` : 'Nenhuma competência vencida para fechar.')
      await qc.invalidateQueries({ queryKey: ['finance'] })
      await qc.invalidateQueries({ queryKey: ['commissions'] })
    },
    onError: (e) => toast.error(errorMessage(e)),
  })

  const label = month ? `${month.slice(5, 7)}/${month.slice(0, 4)}` : ''
  const openConfirm = () => {
    if (!/^\d{4}-\d{2}$/.test(month)) {
      setMonthError('Escolha o mês a fechar.')
      return
    }
    if (`${month}-01` >= `${todayIn().slice(0, 7)}-01`) {
      setMonthError('Só é possível fechar meses já encerrados (America/Sao_Paulo).')
      return
    }
    setMonthError(null)
    setHint(null)
    setConfirm(true)
  }

  return (
    <div className="flex flex-col gap-6">
      <PageHeader title="Fechamento mensal" description="Snapshot com hash e bloqueio de lançamentos do período" breadcrumbs={[{ label: 'Financeiro', to: '/financeiro' }, { label: 'Fechamento' }]} />
      <Can permission="finance.manage">
        <Card>
          <CardHeader>
            <div className="flex flex-col gap-1">
              <CardTitle>Fechar um mês</CardTitle>
              <CardDescription>Ação irreversível: depois de fechado, o período não aceita novos lançamentos (ajustes entram no mês aberto).</CardDescription>
            </div>
            <LockKeyhole aria-hidden="true" className="size-5 text-text-muted" />
          </CardHeader>
          {hint ? (
            <Alert variant="warning">
              <AlertDescription>{hint}</AlertDescription>
            </Alert>
          ) : null}
          <div className="flex flex-wrap items-end gap-3">
            <Field invalid={!!monthError} className="w-full max-w-[220px]">
              <FieldLabel required>Mês</FieldLabel>
              <Input type="month" value={month} onChange={(e) => setMonth(e.target.value)} />
              <FieldError>{monthError}</FieldError>
            </Field>
            <Button variant="destructive" onClick={openConfirm}>
              <Lock aria-hidden="true" />
              Fechar mês
            </Button>
            <Button variant="secondary" onClick={() => closeDue.mutate()} loading={closeDue.isPending} loadingText="Fechando…">
              Fechar competências de comissão vencidas
            </Button>
          </div>
          <Field>
            <FieldDescription>
              As comissões fecham no último dia do mês às 23:59:59 (America/Sao_Paulo) e são liberadas no dia 1; o fechamento contábil exige a competência de comissões fechada.
            </FieldDescription>
          </Field>
        </Card>
      </Can>
      <Card>
        <CardHeader>
          <CardTitle>Meses fechados</CardTitle>
        </CardHeader>
        {q.isLoading ? (
          <Skeleton className="h-40 w-full" />
        ) : q.error ? (
          <ErrorState error={q.error} onRetry={() => void q.refetch()} size="compact" />
        ) : !q.data?.items.length ? (
          <EmptyState size="compact" headingLevel="h3" icon={Lock} title="Nenhum mês fechado ainda." />
        ) : (
          <Table>
            <caption className="sr-only">Meses fechados</caption>
            <TableHeader>
              <TableRow className="h-11 hover:bg-transparent">
                <TableHead scope="col">Mês</TableHead>
                <TableHead scope="col">Situação</TableHead>
                <TableHead scope="col">Fechado em</TableHead>
                <TableHead scope="col">Por</TableHead>
                <TableHead scope="col" numeric>
                  Lançamentos
                </TableHead>
                <TableHead scope="col">SHA-256</TableHead>
                <TableHead scope="col" className="text-right">
                  Ações
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {q.data.items.map((c: Closing) => (
                <TableRow key={c.id}>
                  <TableCell className="font-semibold">{formatIsoMonth(c.period_start)}</TableCell>
                  <TableCell>
                    <ToneBadge tone="success">Fechado</ToneBadge>
                  </TableCell>
                  <TableCell className="tabular-nums">{formatDateTime(c.closed_at)}</TableCell>
                  <TableCell>{nameOf(c.closed_by)}</TableCell>
                  <TableCell numeric>{formatNumber(c.transaction_count)}</TableCell>
                  <TableCell>
                    <span className="font-mono text-xs" title={c.snapshot_sha256}>
                      {c.snapshot_sha256.slice(0, 12)}…
                    </span>
                  </TableCell>
                  <TableCell className="text-right">
                    <Button variant="secondary" onClick={() => setOpenId(c.id)} aria-label={`Ver snapshot de ${formatIsoMonth(c.period_start)}`}>
                      Snapshot
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Card>
      <ConfirmDialog
        open={confirm}
        onOpenChange={setConfirm}
        destructive
        title={`Fechar ${label}?`}
        description="O snapshot (balancete + DRE) é gravado com hash SHA-256 e o período passa a recusar lançamentos."
        consequences={['Não é possível reabrir o mês.', 'Ajustes posteriores entram na competência aberta.']}
        requireText={`FECHAR ${label}`}
        confirmHint="Digitar o mês evita fechar a competência errada — o fechamento não pode ser desfeito."
        confirmLabel="Fechar mês"
        onConfirm={async () => {
          try {
            await financeApi.closePeriod(`${month}-01`)
          } catch (e) {
            if (isApiError(e) && e.code === 'COMMISSION_COMPETENCE_OPEN') {
              setHint(`${e.message} Use “Fechar competências de comissão vencidas” e tente de novo.`)
            }
            throw e
          }
          toast.success(`Mês ${label} fechado.`)
          await qc.invalidateQueries({ queryKey: ['finance'] })
        }}
      />
      <ClosingSheet id={openId} onOpenChange={(o) => !o && setOpenId(null)} />
    </div>
  )
}
