/**
 * Detalhamento do PREÇO CALCULADO PELO SERVIDOR (motor de preços W-CAMP). O frontend nunca
 * calcula preço: só exibe as linhas/totais devolvidos pela cotação (`quote`).
 * Margem (custos e componentes) aparece apenas para a equipe RD (`pricing.view`).
 */
import type { ReactNode } from 'react'
import { AlertTriangle, Calculator, ShieldAlert } from 'lucide-react'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { EMPTY_VALUE, formatBps, formatCents, formatNumber } from '@/lib/format'
import { cn } from '@/lib/utils'
import { regionLabel } from '@/pages/campaigns/lib/labels'
import { cityKeyLabel } from '@/pages/campaigns/lib/rules'
import { formatPlainDate } from '@/pages/campaigns/lib/format'
import type { MarginBreakdown, Quote } from '@/pages/campaigns/lib/types'

export function QuoteBreakdown({ quote, showGroups = true, title = 'Preço calculado pelo servidor' }: { quote: Quote; showGroups?: boolean; title?: string }) {
  const inp = quote.inputs
  return (
    <section aria-label={title} className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2 text-xs text-text-muted">
        <Calculator aria-hidden="true" className="size-4" />
        <span>
          {title}
          {quote.price_table?.version ? ` · tabela de preços v${quote.price_table.version}` : ''}
          {quote.quote_hash ? ` · cotação ${quote.quote_hash.slice(0, 10)}` : ''}
        </span>
      </div>
      {inp ? (
        <dl className="m-0 grid grid-cols-2 gap-x-4 gap-y-2 text-sm md:grid-cols-4">
          <Def term="Telas" value={formatNumber(inp.screens_count)} />
          <Def term="Dias contratados" value={formatNumber(inp.contracted_days)} />
          <Def term="Período" value={`${formatPlainDate(inp.start_date)} a ${formatPlainDate(inp.end_date)}`} />
          <Def term="Inserções previstas" value={quote.insertions ? formatNumber(quote.insertions.total) : EMPTY_VALUE} />
        </dl>
      ) : null}
      <Table>
        <caption className="sr-only">Composição do preço</caption>
        <TableHeader>
          <TableRow className="h-11 hover:bg-transparent">
            <TableHead scope="col">Item</TableHead>
            <TableHead scope="col" numeric>
              Valor
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {quote.lines.map((l, i) => (
            <TableRow key={`${l.code}-${i}`}>
              <TableCell>{l.description}</TableCell>
              <TableCell numeric className={cn('tabular-nums', l.amount_cents < 0 && 'text-success')}>
                {formatCents(l.amount_cents)}
              </TableCell>
            </TableRow>
          ))}
          <TableRow className="hover:bg-transparent">
            <TableCell className="font-bold">Total</TableCell>
            <TableCell numeric className="text-base font-bold tabular-nums" data-testid="quote-total">
              {formatCents(quote.total_cents)}
            </TableCell>
          </TableRow>
        </TableBody>
      </Table>
      {showGroups && quote.groups?.length ? (
        <details className="rounded-lg border border-border p-3 text-sm">
          <summary className="min-h-11 cursor-pointer content-center font-semibold">Distribuição por região</summary>
          <Table>
            <caption className="sr-only">Distribuição do valor de mídia por região</caption>
            <TableHeader>
              <TableRow className="h-11 hover:bg-transparent">
                <TableHead scope="col">Região</TableHead>
                <TableHead scope="col">UF</TableHead>
                <TableHead scope="col">Cidade</TableHead>
                <TableHead scope="col">Classe</TableHead>
                <TableHead scope="col" numeric>
                  Telas
                </TableHead>
                <TableHead scope="col" numeric>
                  Mídia
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {quote.groups.map((g, i) => (
                <TableRow key={i}>
                  <TableCell>{regionLabel(g.region)}</TableCell>
                  <TableCell>{g.state ?? EMPTY_VALUE}</TableCell>
                  <TableCell>{cityKeyLabel(g.city_key)}</TableCell>
                  <TableCell>{g.screen_class === 'PREMIUM' ? 'Premium' : 'Padrão'}</TableCell>
                  <TableCell numeric className="tabular-nums">
                    {formatNumber(g.screens)}
                  </TableCell>
                  <TableCell numeric className="tabular-nums">
                    {formatCents(g.media_cents)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </details>
      ) : null}
      {quote.margin ? <MarginTable margin={quote.margin} /> : null}
    </section>
  )
}

function Def({ term, value }: { term: string; value: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col">
      <dt className="text-xs text-text-muted">{term}</dt>
      <dd className="m-0 tabular-nums text-text">{value}</dd>
    </div>
  )
}

const MARGIN_ROWS: Array<[keyof MarginBreakdown, string]> = [
  ['gateway_cents', 'Gateway de pagamento'],
  ['establishment_commission_cents', 'Comissão dos estabelecimentos (pool)'],
  ['seller_commission_cents', 'Comissão do vendedor'],
  ['taxes_cents', 'Impostos'],
  ['server_cents', 'Servidor'],
  ['equipment_cents', 'Equipamento'],
  ['designer_cents', 'Designer'],
  ['direct_costs_cents', 'Custos diretos'],
]

/** Margem com todos os componentes; negativa = bloqueio; abaixo do mínimo (40%) = alerta. */
export function MarginTable({ margin }: { margin: MarginBreakdown }) {
  return (
    <section aria-label="Margem" className="flex flex-col gap-3">
      <h3 className="text-title-card">Margem (visível somente para a equipe RD)</h3>
      {margin.status === 'NEGATIVE' ? (
        <Alert variant="danger">
          <ShieldAlert aria-hidden="true" />
          <div>
            <AlertTitle>Bloqueado: margem negativa</AlertTitle>
            <AlertDescription>Esta configuração não pode ser vendida. Revise preços, descontos ou custos.</AlertDescription>
          </div>
        </Alert>
      ) : margin.status === 'BELOW_MIN' ? (
        <Alert variant="warning">
          <AlertTriangle aria-hidden="true" />
          <div>
            <AlertTitle>Margem abaixo do mínimo de {formatBps(margin.min_margin_bps, 0)}</AlertTitle>
            <AlertDescription>A venda é permitida, mas fica registrada com alerta na auditoria.</AlertDescription>
          </div>
        </Alert>
      ) : null}
      <Table>
        <caption className="sr-only">Componentes da margem</caption>
        <TableBody>
          <TableRow>
            <TableCell>Receita</TableCell>
            <TableCell numeric className="tabular-nums">
              {formatCents(margin.revenue_cents)}
            </TableCell>
          </TableRow>
          {MARGIN_ROWS.map(([k, label]) => (
            <TableRow key={k}>
              <TableCell>
                {label}
                {k === 'establishment_commission_cents' ? ` (${formatBps(margin.commission_pool_bps)})` : ''}
              </TableCell>
              <TableCell numeric className="tabular-nums">
                − {formatCents(margin[k] as number)}
              </TableCell>
            </TableRow>
          ))}
          <TableRow>
            <TableCell className="font-semibold">Custos totais</TableCell>
            <TableCell numeric className="font-semibold tabular-nums">
              − {formatCents(margin.total_costs_cents)}
            </TableCell>
          </TableRow>
          <TableRow className="hover:bg-transparent">
            <TableCell className="font-bold">Margem</TableCell>
            <TableCell
              numeric
              data-testid="margin-value"
              className={cn('font-bold tabular-nums', margin.status === 'NEGATIVE' ? 'text-danger' : margin.status === 'BELOW_MIN' ? 'text-warning' : 'text-success')}
            >
              {formatCents(margin.margin_cents)} ({formatBps(margin.margin_bps)})
            </TableCell>
          </TableRow>
        </TableBody>
      </Table>
    </section>
  )
}
