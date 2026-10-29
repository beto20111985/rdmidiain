/** Renderização genérica de um conjunto de dados de relatório (colunas tipadas pela API). */
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { formatIsoDate } from '@/components/finance/period'
import { EMPTY_VALUE, formatBps, formatCents, formatDateTime, formatNumber } from '@/lib/format'
import type { ReportColumn, ReportData } from './api'

function formatReportValue(col: ReportColumn, v: string | number | null | undefined) {
  if (v === null || v === undefined || v === '') return EMPTY_VALUE
  switch (col.kind) {
    case 'int':
      return typeof v === 'number' ? formatNumber(v) : String(v)
    case 'money':
      return typeof v === 'number' ? formatCents(v) : String(v)
    case 'bps':
      return typeof v === 'number' ? formatBps(v, 1) : String(v)
    case 'date':
      return formatIsoDate(String(v))
    case 'datetime':
      return formatDateTime(String(v))
    default:
      if (col.key.endsWith('_cents') && typeof v === 'number') return formatCents(v)
      return String(v)
  }
}

const NUMERIC = new Set(['int', 'money', 'bps'])

export function ReportTable({ data, maxRows = 500 }: { data: ReportData; maxRows?: number }) {
  const rows = data.rows.slice(0, maxRows)
  return (
    <div className="flex flex-col gap-2">
      <div className="overflow-x-auto" role="region" aria-label={`Tabela: ${data.title}`} tabIndex={0}>
        <Table>
          <caption className="sr-only">{data.title}</caption>
          <TableHeader>
            <TableRow className="h-11 hover:bg-transparent">
              {data.columns.map((c) => (
                <TableHead key={c.key} scope="col" numeric={NUMERIC.has(c.kind)}>
                  {c.label}
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((r, i) => (
              <TableRow key={i}>
                {data.columns.map((c) => (
                  <TableCell key={c.key} numeric={NUMERIC.has(c.kind)}>
                    {formatReportValue(c, r[c.key])}
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      {data.rows.length > maxRows ? (
        <p className="m-0 text-xs text-text-muted">
          Mostrando {formatNumber(maxRows)} de {formatNumber(data.rows.length)} linhas. Exporte para ver tudo.
        </p>
      ) : null}
    </div>
  )
}
