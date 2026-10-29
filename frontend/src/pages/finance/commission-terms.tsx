/**
 * Termos de comissão POR PAGAMENTO de uma campanha (W-FIN 0006 —
 * `GET /financial/commissions/campaigns/{id}`): contratação inicial, renovação e alteração,
 * cada uma com o seu período, pool e situação.
 * - Visão FULL (financeiro/equipe RD com escopo global): cobrança, modelo, receita elegível, pool,
 *   liberado, apropriado e estornos.
 * - Visão ESTABLISHMENT: só os termos de que a unidade participa e só a comissão DELA — sem
 *   cobrança, receita, pool, modelo, campanha ou anunciante (a tela nem pede esses dados).
 * Fora do escopo ou sem termos ⇒ 404, tratado como "sem termos" (não revela a campanha).
 */
import * as React from 'react'
import { useQuery } from '@tanstack/react-query'
import { FileText } from 'lucide-react'
import { Sheet, SheetBody, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState, ErrorState } from '@/components/patterns/states'
import { DetailList } from '@/components/finance/detail-list'
import { StatusPill } from '@/components/finance/status-pill'
import { COMMISSION_MODEL_LABEL, COMMISSION_TERM_KIND_LABEL, COMMISSION_TERM_STATUS } from '@/components/finance/labels'
import { formatIsoDate } from '@/components/finance/period'
import { isApiError } from '@/lib/errors'
import { qk } from '@/lib/queryKeys'
import { EMPTY_VALUE, formatBps, formatCents, formatDateTime, formatNumber } from '@/lib/format'
import { financeApi, type CampaignCommissionTerms, type CommissionTermFull } from './api'

export interface TermsTarget {
  campaignId: string
  /** Rótulo exibido no título (nome da campanha para a RD; "Contratação N" para a unidade). */
  label: string
}

function kindLabel(kind: string) {
  return COMMISSION_TERM_KIND_LABEL[kind] ?? kind
}

function TermsBody({ data }: { data: CampaignCommissionTerms }) {
  if (!data.terms.length) {
    return <EmptyState size="compact" headingLevel="h3" icon={FileText} title="Nenhum termo de comissão registrado." />
  }
  const full = data.visibility === 'FULL'
  return (
    <div className="flex flex-col gap-4">
      <section aria-label="Totais da campanha" className="flex flex-col gap-2">
        <h3 className="text-title-card text-text">Totais</h3>
        {data.visibility === 'FULL' ? (
          <DetailList
            columns={2}
            items={[
              { label: 'Receita elegível', value: formatCents(data.totals.eligible_revenue_cents), numeric: true },
              { label: 'Pool', value: formatCents(data.totals.pool_cents), numeric: true },
              { label: 'Liberado nos fechamentos', value: formatCents(data.totals.released_cents), numeric: true },
              { label: 'Comissão apropriada (líquida)', value: formatCents(data.totals.commission_cents), numeric: true },
              { label: 'Estornos', value: formatCents(data.totals.clawback_cents), numeric: true },
              { label: 'Exibições válidas', value: formatNumber(data.totals.valid_plays), numeric: true },
            ]}
          />
        ) : (
          <DetailList
            columns={2}
            items={[
              { label: 'Sua comissão (líquida)', value: formatCents(data.totals.commission_cents), numeric: true },
              { label: 'Estornos', value: formatCents(data.totals.clawback_cents), numeric: true },
              { label: 'Exibições válidas nas suas telas', value: formatNumber(data.totals.valid_plays), numeric: true },
            ]}
          />
        )}
      </section>
      <ol className="m-0 flex list-none flex-col gap-3 p-0" aria-label="Termos por pagamento">
        {data.terms.map((t) => {
          const f = full ? (t as CommissionTermFull) : null
          return (
            <li key={t.terms_id} className="flex flex-col gap-3 rounded-lg border border-border p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="text-sm font-semibold text-text">
                  {kindLabel(t.kind)} · {formatIsoDate(t.period_start)} a {formatIsoDate(t.period_end)}
                </h3>
                <StatusPill map={COMMISSION_TERM_STATUS} value={t.status} />
              </div>
              {t.status === 'CANCELLED' && t.cancelled_on ? (
                <p className="m-0 text-xs text-text-muted">Cancelado em {formatIsoDate(t.cancelled_on)} (estorno da contratação).</p>
              ) : null}
              <DetailList
                columns={2}
                items={[
                  ...(f
                    ? [
                        { label: 'Cobrança', value: f.payment_order_id ? <span className="font-mono text-xs">{f.payment_order_id.slice(0, 8)}</span> : EMPTY_VALUE },
                        { label: 'Modelo', value: COMMISSION_MODEL_LABEL[f.model] ?? f.model },
                        { label: 'Pool', value: `${formatBps(f.pool_bps)} · ${formatCents(f.pool_cents)}` },
                        { label: 'Receita elegível', value: formatCents(f.eligible_revenue_cents), numeric: true },
                        { label: 'Liberado', value: formatCents(f.released_cents), numeric: true },
                        {
                          label: 'Exibições válidas / previstas',
                          value: `${formatNumber(f.valid_plays)} / ${f.expected_exhibitions === null ? 'após a 1ª apropriação' : formatNumber(f.expected_exhibitions)}`,
                        },
                      ]
                    : [{ label: 'Exibições válidas nas suas telas', value: formatNumber(t.valid_plays), numeric: true }]),
                  { label: full ? 'Comissão apropriada' : 'Sua comissão', value: formatCents(t.commission_cents), numeric: true },
                  { label: 'Estornos', value: formatCents(t.clawback_cents), numeric: true },
                  ...(f?.registered_at ? [{ label: 'Registrado em', value: formatDateTime(f.registered_at) }] : []),
                ]}
              />
            </li>
          )
        })}
      </ol>
    </div>
  )
}

export function CommissionTermsSheet({ target, onOpenChange }: { target: TermsTarget | null; onOpenChange: (open: boolean) => void }) {
  // Mantém o último alvo durante a animação de saída (o conteúdo não some antes de o painel fechar).
  const [shown, setShown] = React.useState(target)
  if (target && target !== shown) setShown(target)
  const current = target ?? shown
  const q = useQuery({
    queryKey: qk.sub('commissions', 'terms', current?.campaignId ?? ''),
    queryFn: () => financeApi.campaignCommissionTerms(current!.campaignId),
    enabled: !!target,
    retry: (n, e) => !(isApiError(e) && (e.status === 404 || e.status === 403)) && n < 2,
  })
  const notFound = isApiError(q.error) && q.error.status === 404
  return (
    <Sheet open={!!target} onOpenChange={onOpenChange}>
      <SheetContent>
        <SheetHeader>
          <SheetTitle>Termos de comissão{current ? ` — ${current.label}` : ''}</SheetTitle>
          <SheetDescription>
            Cada pagamento (contratação inicial, renovação ou alteração) gera um termo com período e pool próprios.
          </SheetDescription>
        </SheetHeader>
        <SheetBody className="flex flex-col gap-4 pb-6">
          {q.isLoading ? (
            <Skeleton className="h-48 w-full" />
          ) : notFound ? (
            <EmptyState
              size="compact"
              headingLevel="h3"
              icon={FileText}
              title="Sem termos de comissão para mostrar."
              description="Os termos aparecem depois da primeira apropriação de exibições válidas."
            />
          ) : q.error ? (
            <ErrorState error={q.error} onRetry={() => void q.refetch()} size="compact" />
          ) : q.data ? (
            <TermsBody data={q.data} />
          ) : null}
        </SheetBody>
      </SheetContent>
    </Sheet>
  )
}
