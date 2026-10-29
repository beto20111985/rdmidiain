/**
 * CRM → Funil de vendas (P001 §106–§107): quadro por etapa (kanban acessível por teclado —
 * cada cartão tem "Mover para…", sem depender de arrastar) ou lista; quantidades e valores
 * estimados por etapa vêm de `/crm/pipeline`; a mudança de etapa grava histórico no servidor.
 */
import * as React from 'react'
import { useQueries, useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { ArrowRightLeft, Filter } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { KpiCard } from '@/components/patterns/kpi-card'
import { ErrorState } from '@/components/patterns/states'
import { Can } from '@/components/auth/guards'
import { LEAD_STAGES, LEAD_TRANSITIONS } from '@/components/finance/labels'
import { personLabel } from '@/components/finance/names'
import { useAuthStore } from '@/stores/authStore'
import { qk } from '@/lib/queryKeys'
import { EMPTY_VALUE, formatBps, formatCents, formatNumber } from '@/lib/format'
import { crmApi, type Lead } from './api'
import { StageChangeDialog } from './lead-dialogs'
import { LeadSheet } from './lead-sheet'

const COLUMN_LIMIT = 100

export default function PipelinePage() {
  const meId = useAuthStore((s) => s.me?.user.id)
  const [sp, setSp] = useSearchParams()
  const view = sp.get('visao') === 'lista' ? 'lista' : 'quadro'
  const [moving, setMoving] = React.useState<Lead | null>(null)
  const [openId, setOpenId] = React.useState<string | null>(null)
  const pipeline = useQuery({ queryKey: qk.sub('crm', 'pipeline'), queryFn: crmApi.pipeline })
  const columns = useQueries({
    queries: LEAD_STAGES.map((s) => ({
      queryKey: qk.list('crm', { kind: 'leads', stage: s.value, page_size: COLUMN_LIMIT }),
      queryFn: () => crmApi.leads({ stage: s.value, page_size: COLUMN_LIMIT }),
      enabled: view === 'quadro',
    })),
  })
  const byStage = new Map((pipeline.data?.stages ?? []).map((s) => [s.stage, s]))
  const open = (pipeline.data?.stages ?? []).filter((s) => s.stage !== 'GANHO' && s.stage !== 'PERDIDO')
  const openValue = open.reduce((a, s) => a + s.estimated_value_cents, 0)
  const openCount = open.reduce((a, s) => a + s.count, 0)

  return (
    <div className="flex flex-col gap-6">
      <PageHeader title="Funil de vendas" description="Etapas do funil comercial e valores em negociação" />
      {pipeline.error ? <ErrorState error={pipeline.error} onRetry={() => void pipeline.refetch()} size="compact" /> : null}
      <section aria-label="Resumo do funil" className="grid grid-cols-1 gap-4 md:grid-cols-3 xl:gap-6">
        <KpiCard label="Leads em aberto" value={pipeline.data ? openCount : null} loading={pipeline.isLoading} emphasis="secondary" />
        <KpiCard label="Valor em negociação" value={pipeline.data ? openValue : null} format={formatCents} loading={pipeline.isLoading} emphasis="secondary" description="Soma dos valores estimados dos leads em aberto." />
        <KpiCard
          label="Taxa de conversão"
          value={pipeline.data?.conversion_rate_bps ?? null}
          format={(v) => formatBps(v, 1)}
          loading={pipeline.isLoading}
          emphasis="secondary"
          description={pipeline.data?.conversion_rate_bps === null ? 'Sem leads ganhos ou perdidos ainda.' : 'Ganhos ÷ (ganhos + perdidos).'}
        />
      </section>
      <div className="flex flex-col gap-2">
        <span id="pipe-view" className="text-sm font-semibold text-text">
          Visualização
        </span>
        <ToggleGroup
          type="single"
          value={view}
          aria-labelledby="pipe-view"
          className="self-start"
          onValueChange={(v) =>
            v &&
            setSp(
              (prev) => {
                const n = new URLSearchParams(prev)
                if (v === 'lista') n.set('visao', 'lista')
                else n.delete('visao')
                return n
              },
              { replace: true },
            )
          }
        >
          <ToggleGroupItem value="quadro">Quadro</ToggleGroupItem>
          <ToggleGroupItem value="lista">Lista</ToggleGroupItem>
        </ToggleGroup>
      </div>

      {view === 'lista' ? (
        <Card>
          <CardHeader>
            <CardTitle>Etapas</CardTitle>
          </CardHeader>
          {pipeline.isLoading ? (
            <Skeleton className="h-48 w-full" />
          ) : (
            <Table>
              <caption className="sr-only">Funil por etapa</caption>
              <TableHeader>
                <TableRow className="h-11 hover:bg-transparent">
                  <TableHead scope="col">Etapa</TableHead>
                  <TableHead scope="col" numeric>
                    Leads
                  </TableHead>
                  <TableHead scope="col" numeric>
                    Valor estimado
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {LEAD_STAGES.map((s) => (
                  <TableRow key={s.value}>
                    <TableCell className="font-semibold">{s.label}</TableCell>
                    <TableCell numeric>{formatNumber(byStage.get(s.value)?.count ?? null)}</TableCell>
                    <TableCell numeric>{formatCents(byStage.get(s.value)?.estimated_value_cents ?? null)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </Card>
      ) : (
        <div role="region" aria-label="Quadro do funil" tabIndex={0} className="-mx-1 overflow-x-auto px-1 pb-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[rgb(var(--focus))]">
          <div className="grid grid-cols-1 gap-4 md:auto-cols-[minmax(260px,1fr)] md:grid-flow-col md:grid-cols-none">
            {LEAD_STAGES.map((s, i) => {
              const col = columns[i]
              const leads = col.data?.items ?? []
              const info = byStage.get(s.value)
              const titleId = `stage-${s.value}`
              return (
                <section key={s.value} aria-labelledby={titleId} className="flex min-w-0 flex-col gap-3 rounded-xl border border-border bg-surface p-4">
                  <header className="flex flex-col gap-0.5">
                    <h2 id={titleId} className="text-title-card text-text">
                      {s.label} <span className="tabular-nums text-text-muted">({formatNumber(info?.count ?? null)})</span>
                    </h2>
                    <p className="m-0 text-xs text-text-muted">{formatCents(info?.estimated_value_cents ?? null)} estimados</p>
                  </header>
                  {col.isLoading ? (
                    <Skeleton className="h-24 w-full" />
                  ) : col.error ? (
                    <ErrorState error={col.error} onRetry={() => void col.refetch()} size="compact" />
                  ) : leads.length === 0 ? (
                    <p className="m-0 text-sm text-text-muted">Nenhum lead nesta etapa.</p>
                  ) : (
                    <ul className="m-0 flex list-none flex-col gap-2 p-0">
                      {leads.map((l) => (
                        <li key={l.id} className="flex flex-col gap-2 rounded-lg border border-border bg-surface-raised p-3">
                          <button type="button" className="text-left" onClick={() => setOpenId(l.id)}>
                            <span className="block font-semibold text-link underline-offset-4 hover:underline">{l.name}</span>
                            <span className="block text-xs text-text-muted">
                              {l.company ?? EMPTY_VALUE} · {formatCents(l.estimated_value_cents)}
                            </span>
                            <span className="block text-xs text-text-muted">Responsável: {personLabel(l.owner_name, l.owner_id, meId)}</span>
                          </button>
                          {(LEAD_TRANSITIONS[l.stage] ?? []).length ? (
                            <Can permission="crm.manage">
                              <Button variant="ghost" className="self-start" onClick={() => setMoving(l)} aria-label={`Mover ${l.name} para outra etapa`}>
                                <ArrowRightLeft aria-hidden="true" />
                                Mover para…
                              </Button>
                            </Can>
                          ) : null}
                        </li>
                      ))}
                    </ul>
                  )}
                  {(info?.count ?? 0) > COLUMN_LIMIT ? (
                    <p className="m-0 text-xs text-text-muted">Mostrando {COLUMN_LIMIT} de {formatNumber(info?.count ?? null)}. Use a página Leads para filtrar.</p>
                  ) : null}
                </section>
              )
            })}
          </div>
        </div>
      )}
      <p className="m-0 flex items-center gap-2 text-xs text-text-muted">
        <Filter aria-hidden="true" className="size-4 shrink-0" />
        “Perdido” exige motivo; “Ganho” é registrado automaticamente quando a proposta vira pedido.
      </p>
      <StageChangeDialog lead={moving} onOpenChange={(o) => !o && setMoving(null)} />
      <LeadSheet leadId={openId} onOpenChange={(o) => !o && setOpenId(null)} />
    </div>
  )
}
