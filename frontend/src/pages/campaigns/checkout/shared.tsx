/** Peças compartilhadas do carrinho/checkout e das ações que geram cobrança (renovação, alteração). */
import { Link, useLocation } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { CampaignStatusBadge } from '@/components/campaigns/badges'
import { qk } from '@/lib/queryKeys'
import { isApiError } from '@/lib/errors'
import { formatNumber } from '@/lib/format'
import { campaignsApi } from '../lib/api'
import { describeError, isPayerError, isPriceFloorError, priceFloorText } from '../lib/errorText'

export function CampaignLabel({ id, base }: { id: string; base: string }) {
  const q = useQuery({ queryKey: qk.detail('campaigns', id), queryFn: () => campaignsApi.get(id), staleTime: 60_000 })
  return (
    <span className="flex flex-wrap items-center gap-2">
      <Link className="font-semibold text-link underline-offset-4 [overflow-wrap:anywhere] hover:underline" to={`${base}/${id}`}>
        {q.data?.name ?? `Campanha ${id.slice(0, 8)}`}
      </Link>
      {q.data ? <CampaignStatusBadge status={q.data.status} /> : null}
    </span>
  )
}

const MISSING_LABEL: Record<string, string> = { email: 'e-mail', phone: 'telefone', document: 'CPF/CNPJ' }
const SUPPORT_PATH = '/anunciante/suporte'

interface ScreenProblem {
  reason?: string | null
  message?: string | null
}

/** "Completar meus dados" leva a Minha conta com `voltar` = onde a pessoa está (o carrinho guarda seleção e
 * cupom no endereço), para voltar ao pagamento depois de salvar o telefone. */
function useAccountLink(): string {
  const loc = useLocation()
  return `/minha-conta?voltar=${encodeURIComponent(`${loc.pathname}${loc.search}`)}`
}

function PayerError({ code, message, missing }: { code: string; message: string; missing: string[] }) {
  const accountLink = useAccountLink()
  const notSupported = code === 'PAYER_DOCUMENT_NOT_SUPPORTED_BY_GATEWAY'
  const invalid = code === 'PAYER_DOCUMENT_INVALID'
  // Em "Minha conta" a pessoa corrige o TELEFONE; CPF/CNPJ e e-mail da empresa só pelo suporte.
  const onlyOrgData = invalid || (missing.length > 0 && !missing.includes(MISSING_LABEL.phone))
  const title = notSupported
    ? 'CNPJ alfanumérico ainda não aceito pelo meio de pagamento'
    : invalid
      ? 'CPF/CNPJ do cadastro inválido para pagamento'
      : code === 'PAYER_DATA_INCOMPLETE'
        ? 'Complete o cadastro para pagar'
        : 'Revise os dados do pagador'
  return (
    <Alert variant="warning" data-testid="checkout-error-422" data-code={code}>
      <div>
        <AlertTitle>{title}</AlertTitle>
        <AlertDescription>
          <p className="m-0">
            {message}
            {missing.length ? ` Faltando: ${missing.join(', ')}.` : ''}
          </p>
          {notSupported ? (
            <p className="m-0 mt-1">Seu cadastro está correto — não altere o CNPJ. O suporte ou o comercial da RD conclui o pagamento com você.</p>
          ) : null}
          <div className="mt-2 flex flex-wrap gap-2">
            {notSupported ? (
              <Button variant="secondary" asChild>
                <Link to={SUPPORT_PATH}>Falar com o suporte</Link>
              </Button>
            ) : (
              <>
                {onlyOrgData ? null : (
                  <Button variant="secondary" asChild>
                    <Link to={accountLink}>Completar meus dados</Link>
                  </Button>
                )}
                <Button variant="tertiary" asChild>
                  <Link to={SUPPORT_PATH}>Atualizar dados da empresa com o suporte</Link>
                </Button>
              </>
            )}
          </div>
          <p className="m-0 mt-2 text-xs text-text-muted">Nenhuma cobrança foi gerada e nada do que você preencheu foi perdido.</p>
        </AlertDescription>
      </div>
    </Alert>
  )
}

/**
 * Erro do checkout (e de renovação/alteração paga) traduzido para o anunciante:
 * 409 inventário indisponível (motivos por tela, agrupados — sem identificar estabelecimentos),
 * 422/400 `PAYER_*` dados/documento do pagador (com o caminho para resolver), piso de preço RD
 * (mínimo em BRL), 503 dependência indisponível (nada foi cobrado/reservado) e demais mensagens.
 */
export function CheckoutError({ error }: { error: unknown }) {
  if (!error) return null
  if (isApiError(error) && error.code === 'INVENTORY_UNAVAILABLE') {
    const d = error.details as { available?: number; requested?: number; screens?: ScreenProblem[] }
    const groups = new Map<string, number>()
    for (const s of d.screens ?? []) {
      const k = s.message || s.reason || 'Sem capacidade no período'
      groups.set(k, (groups.get(k) ?? 0) + 1)
    }
    return (
      <Alert variant="danger" data-testid="checkout-error-409">
        <div>
          <AlertTitle>Inventário indisponível</AlertTitle>
          <AlertDescription>
            <p className="m-0">
              {error.message} {d.requested !== undefined ? `Telas disponíveis: ${formatNumber(d.available ?? 0)} de ${formatNumber(d.requested)}.` : ''}
            </p>
            {groups.size ? (
              <ul className="m-0 mt-2 list-disc pl-5">
                {[...groups.entries()].map(([msg, n]) => (
                  <li key={msg}>
                    {n} tela(s): {msg}
                  </li>
                ))}
              </ul>
            ) : null}
            <p className="m-0 mt-2">Ajuste a segmentação, as datas ou a frequência e tente novamente. Nenhuma cobrança foi gerada.</p>
          </AlertDescription>
        </div>
      </Alert>
    )
  }
  if (isApiError(error) && (error.status === 422 || error.status === 400) && isPayerError(error)) {
    const missing = ((error.details.missing as string[] | undefined) ?? []).map((m) => MISSING_LABEL[m] ?? m)
    return <PayerError code={error.code} message={error.message} missing={missing} />
  }
  if (isPriceFloorError(error)) {
    return (
      <Alert variant="danger" data-testid="checkout-error-floor">
        <div>
          <AlertTitle>Preço abaixo do mínimo permitido</AlertTitle>
          <AlertDescription>{priceFloorText(error)} Fale com o comercial da RD.</AlertDescription>
        </div>
      </Alert>
    )
  }
  if (isApiError(error) && error.status === 503) {
    return (
      <Alert variant="danger" data-testid="checkout-error-503">
        <div>
          <AlertTitle>Pagamento indisponível no momento</AlertTitle>
          <AlertDescription>
            Um serviço necessário (inventário ou meio de pagamento) está fora do ar. Nada foi cobrado nem reservado. Tente novamente em alguns minutos.
          </AlertDescription>
        </div>
      </Alert>
    )
  }
  return (
    <Alert variant="danger">
      <div>
        <AlertTitle>Não foi possível gerar a cobrança</AlertTitle>
        <AlertDescription>
          {describeError(error)}
          {isApiError(error) && error.requestId ? <span className="block text-xs text-text-muted">Código para o suporte: {error.requestId}</span> : null}
        </AlertDescription>
      </div>
    </Alert>
  )
}
