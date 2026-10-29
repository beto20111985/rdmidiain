/**
 * Situação da integração CloudPayPag (W-FIN `financial/provider/status`, sem segredos).
 * Capacidades que a API documentada não oferece (boleto, consulta/listagem, estorno,
 * cancelamento, contestação, idempotência de saque) aparecem como
 * "BLOQUEADO POR DEPENDÊNCIA EXTERNA" — nunca como disponíveis.
 */
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, PlugZap } from 'lucide-react'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Skeleton } from '@/components/ui/skeleton'
import { ErrorState } from '@/components/patterns/states'
import { ToneBadge } from '@/components/finance/status-pill'
import { DetailList } from '@/components/finance/detail-list'
import { PROVIDER_CAPABILITY_LABEL } from '@/components/finance/labels'
import { qk } from '@/lib/queryKeys'
import { financeApi } from './api'

export function ProviderStatusCard({ compact = false }: { compact?: boolean }) {
  const q = useQuery({ queryKey: qk.sub('finance', 'provider-status'), queryFn: financeApi.providerStatus })
  const s = q.data
  const caps = Object.entries(s?.capabilities ?? {})
  const blocked = caps.filter(([, v]) => v !== 'DISPONÍVEL')
  const available = caps.filter(([, v]) => v === 'DISPONÍVEL')
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Provedor de pagamentos (CloudPayPag)</CardTitle>
          <CardDescription>Situação da integração desta instância. Nenhum segredo é exibido.</CardDescription>
        </div>
        <PlugZap aria-hidden="true" className="size-5 text-text-muted" />
      </CardHeader>
      {q.isLoading ? (
        <Skeleton className="h-28 w-full" />
      ) : q.error ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} size="compact" />
      ) : s ? (
        <div className="flex flex-col gap-4">
          {s.fake_warning ? (
            <Alert variant="warning">
              <AlertTriangle aria-hidden="true" />
              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <AlertTitle>{s.fake_warning}</AlertTitle>
                <AlertDescription>Cobranças e saques deste ambiente são simulados e não movimentam dinheiro.</AlertDescription>
              </div>
            </Alert>
          ) : null}
          <DetailList
            columns={compact ? 2 : 3}
            items={[
              { label: 'Modo', value: s.mode === 'FAKE' ? 'Simulado (desenvolvimento)' : s.mode === 'LIVE' ? 'Produção' : s.mode },
              { label: 'Validação externa', value: s.external_validation },
              { label: 'Credencial configurada', value: s.token_configured ? 'Sim' : 'Não' },
              { label: 'Segredo do webhook configurado', value: s.webhook_secret_configured ? 'Sim' : 'Não' },
              ...(compact ? [] : [{ label: 'Instância', value: s.instance_slug ?? '—' }, { label: 'Subconta', value: s.subconta_id ?? '—' }]),
            ]}
          />
          {blocked.length ? (
            <section aria-labelledby="cap-blocked" className="flex flex-col gap-2">
              <h3 id="cap-blocked" className="text-title-card text-text">
                Indisponível na API do provedor
              </h3>
              <ul className="m-0 flex list-none flex-col gap-2 p-0">
                {blocked.map(([k, v]) => (
                  <li key={k} className="flex flex-wrap items-center justify-between gap-2 text-sm">
                    <span>{PROVIDER_CAPABILITY_LABEL[k] ?? k}</span>
                    <ToneBadge tone="danger">{v}</ToneBadge>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
          {!compact && available.length ? (
            <section aria-labelledby="cap-ok" className="flex flex-col gap-2">
              <h3 id="cap-ok" className="text-title-card text-text">
                Disponível
              </h3>
              <ul className="m-0 flex list-none flex-col gap-2 p-0">
                {available.map(([k]) => (
                  <li key={k} className="flex flex-wrap items-center justify-between gap-2 text-sm">
                    <span>{PROVIDER_CAPABILITY_LABEL[k] ?? k}</span>
                    <ToneBadge tone="success">Disponível</ToneBadge>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </div>
      ) : null}
    </Card>
  )
}
