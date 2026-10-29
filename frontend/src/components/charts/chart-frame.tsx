/**
 * Moldura acessível de gráfico (DS §11): título, resumo textual SEMPRE visível e
 * alternância para tabela equivalente ("Ver tabela"). O SVG do gráfico fica oculto
 * para leitores de tela (o resumo + a tabela carregam a informação).
 * Estados: carregando (esqueleto), erro (tentar novamente), vazio (mensagem do DS §17).
 */
import * as React from 'react'
import { BarChart3, Table2 } from 'lucide-react'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { EmptyState, ErrorState, DemoBadge } from '@/components/patterns/states'

export interface ChartTableColumn<Row> {
  key: string
  label: string
  numeric?: boolean
  render: (row: Row) => React.ReactNode
}

export interface ChartFrameProps<Row> {
  title: string
  /** Subtítulo (ex.: período e fuso). */
  description?: string
  /** Resumo textual obrigatório (o que o gráfico mostra, em uma ou duas frases). */
  summary: string
  table: { columns: ChartTableColumn<Row>[]; rows: Row[]; caption?: string }
  loading?: boolean
  error?: unknown
  onRetry?: () => void
  empty?: boolean
  emptyText?: string
  demo?: boolean
  actions?: React.ReactNode
  legend?: React.ReactNode
  headingLevel?: 'h2' | 'h3'
  className?: string
  children: React.ReactNode
}

export function ChartFrame<Row>({
  title,
  description,
  summary,
  table,
  loading,
  error,
  onRetry,
  empty,
  emptyText = 'Nenhum dado registrado neste período.',
  demo,
  actions,
  legend,
  headingLevel = 'h2',
  className,
  children,
}: ChartFrameProps<Row>) {
  const [showTable, setShowTable] = React.useState(false)
  const id = React.useId()
  const titleId = `${id}-title`
  const summaryId = `${id}-summary`
  const ready = !loading && !error && !empty

  return (
    <Card className={className}>
      <CardHeader>
        <div className="flex min-w-0 flex-col gap-0.5">
          <div className="flex flex-wrap items-center gap-2">
            <CardTitle as={headingLevel} id={titleId}>
              {title}
            </CardTitle>
            {demo ? <DemoBadge /> : null}
          </div>
          {description ? <CardDescription>{description}</CardDescription> : null}
        </div>
        <div className="flex items-center gap-2">
          {actions}
          {ready ? (
            <Button variant="ghost" aria-pressed={showTable} onClick={() => setShowTable((v) => !v)}>
              {showTable ? <BarChart3 aria-hidden="true" /> : <Table2 aria-hidden="true" />}
              {showTable ? 'Ver gráfico' : 'Ver tabela'}
            </Button>
          ) : null}
        </div>
      </CardHeader>

      {loading ? (
        <div role="status">
          <span className="sr-only">Carregando {title}…</span>
          <Skeleton className="h-[240px] w-full rounded-lg" />
        </div>
      ) : error ? (
        <ErrorState error={error} onRetry={onRetry} size="compact" />
      ) : empty ? (
        <EmptyState title={emptyText} size="compact" headingLevel="h3" />
      ) : (
        <figure aria-labelledby={titleId} aria-describedby={summaryId} className="m-0 flex min-w-0 flex-col gap-3">
          {showTable ? (
            <Table>
              {table.caption ? <caption className="sr-only">{table.caption}</caption> : null}
              <TableHeader>
                <TableRow className="h-11 hover:bg-transparent">
                  {table.columns.map((c) => (
                    <TableHead key={c.key} numeric={c.numeric} scope="col">
                      {c.label}
                    </TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {table.rows.map((row, i) => (
                  <TableRow key={i}>
                    {table.columns.map((c) => (
                      <TableCell key={c.key} numeric={c.numeric}>
                        {c.render(row)}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : (
            <div aria-hidden="true" className="min-w-0">
              {children}
            </div>
          )}
          {legend && !showTable ? legend : null}
          <figcaption id={summaryId} className="text-xs text-text-muted">
            {summary}
          </figcaption>
        </figure>
      )}
    </Card>
  )
}

/** Legenda com chave de linha (mesma marca do gráfico) e texto em token de texto. */
export function ChartLegend({
  items,
}: {
  items: Array<{ label: string; color: string; dash?: string; kind?: 'line' | 'swatch'; value?: string }>
}) {
  return (
    <ul className="m-0 flex list-none flex-wrap gap-x-5 gap-y-2 p-0 text-xs text-text-muted">
      {items.map((it) => (
        <li key={it.label} className="flex items-center gap-2">
          <svg width="20" height="10" aria-hidden="true" className="shrink-0">
            {it.kind === 'swatch' ? (
              <rect x="5" y="0" width="10" height="10" rx="2" fill={it.color} />
            ) : (
              <line x1="0" y1="5" x2="20" y2="5" stroke={it.color} strokeWidth="2" strokeDasharray={it.dash} strokeLinecap="round" />
            )}
          </svg>
          <span className="text-text">{it.label}</span>
          {it.value ? <span className="tabular-nums">{it.value}</span> : null}
        </li>
      ))}
    </ul>
  )
}
