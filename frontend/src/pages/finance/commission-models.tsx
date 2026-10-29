/**
 * Explicação dos modelos de comissão A (pool fixo) e B (receita atribuída) com os exemplos
 * CALCULADOS PELO BACKEND com o mesmo algoritmo da apropriação (P001 §55; REQ-COM-002).
 */
import { useQuery } from '@tanstack/react-query'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Skeleton } from '@/components/ui/skeleton'
import { ErrorState } from '@/components/patterns/states'
import { qk } from '@/lib/queryKeys'
import { formatBps, formatCents } from '@/lib/format'
import { financeApi } from './api'

const MODEL_KEYS: Record<string, string> = { A_POOL: 'A_POOL', B_ATTRIBUTED: 'B_ATTRIBUTED' }

export function CommissionModelsCard({ showExamples = true }: { showExamples?: boolean }) {
  const q = useQuery({ queryKey: qk.sub('commissions', 'models'), queryFn: financeApi.commissionModels, staleTime: 30 * 60 * 1000 })
  const m = q.data
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Como a comissão é calculada</CardTitle>
          <CardDescription>
            {m ? `Pool atual: ${formatBps(m.pool_bps)} da receita elegível de mídia. Exemplos sobre receita de ${formatCents(m.base_revenue_cents)}.` : 'Modelos A e B'}
          </CardDescription>
        </div>
      </CardHeader>
      {q.isLoading ? (
        <Skeleton className="h-40 w-full" />
      ) : q.error ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} size="compact" />
      ) : m ? (
        <div className="flex flex-col gap-5">
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {Object.entries(m.models).map(([key, info]) => (
              <section key={key} aria-label={info.name} className="flex flex-col gap-2 rounded-lg border border-border p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <h3 className="text-title-card text-text">{info.name}</h3>
                  {m.current_model === MODEL_KEYS[key] || m.current_model === key ? <Badge tone="info">Em uso</Badge> : null}
                </div>
                <p className="m-0 text-sm text-text-muted">{info.description}</p>
              </section>
            ))}
          </div>
          <ul className="m-0 flex list-disc flex-col gap-1 pl-5 text-sm text-text">
            {m.rules.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
          {showExamples && m.examples.length ? (
            <Table>
              <caption className="text-left text-sm font-semibold text-text">Exemplos (calculados pelo servidor)</caption>
              <TableHeader>
                <TableRow className="h-11 hover:bg-transparent">
                  <TableHead scope="col">Cenário</TableHead>
                  <TableHead scope="col" numeric>
                    Pool
                  </TableHead>
                  <TableHead scope="col" numeric>
                    Modelo A: pago
                  </TableHead>
                  <TableHead scope="col" numeric>
                    A: fica com a RD
                  </TableHead>
                  <TableHead scope="col" numeric>
                    Modelo B: pago
                  </TableHead>
                  <TableHead scope="col" numeric>
                    B: fica com a RD
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {m.examples.map((e) => (
                  <TableRow key={e.scenario}>
                    <TableCell>
                      <div className="flex flex-col">
                        <span>{e.scenario}</span>
                        <span className="text-xs text-text-muted">
                          A por tela: {e.model_a.per_screen_cents.slice(0, 4).map((c) => formatCents(c)).join(', ')}
                          {e.model_a.per_screen_cents.length > 4 ? '…' : ''}
                        </span>
                      </div>
                    </TableCell>
                    <TableCell numeric>{formatCents(e.model_a.pool_cents)}</TableCell>
                    <TableCell numeric>{formatCents(e.model_a.total_cents)}</TableCell>
                    <TableCell numeric>{formatCents(e.model_a.rd_residue_cents)}</TableCell>
                    <TableCell numeric>{formatCents(e.model_b.total_cents)}</TableCell>
                    <TableCell numeric>{formatCents(e.model_b.rd_residue_cents)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : null}
        </div>
      ) : null}
    </Card>
  )
}
