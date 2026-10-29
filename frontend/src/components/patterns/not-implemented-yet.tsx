/**
 * TEMPORÁRIO — marcador de página ainda não construída (AÇÃO 002). Claramente identificado,
 * sem nenhum dado (nem fictício). Os agentes de páginas o substituem preenchendo `load` em
 * src/app/routes.ts; o coordenador garante que nenhum sobreviva à entrega final
 * (busque por `data-testid="not-implemented-yet"` / `NotImplementedYet`).
 */
import { Construction } from 'lucide-react'
import type { AppRoute } from '@/app/routes'
import { Badge } from '@/components/ui/badge'
import { Card } from '@/components/ui/card'

export function NotImplementedYet({ route }: { route: AppRoute }) {
  return (
    <Card data-testid="not-implemented-yet" className="mx-auto w-full max-w-3xl items-start gap-5 border-dashed">
      <div className="flex flex-wrap items-center gap-3">
        <span aria-hidden="true" className="inline-flex size-12 items-center justify-center rounded-lg border border-border bg-surface-hover text-warning">
          <Construction className="size-6" />
        </span>
        <Badge tone="warning">Página em construção</Badge>
      </div>
      <div className="flex flex-col gap-2">
        <h2 className="text-title-section text-text">Esta página ainda não foi implementada</h2>
        <p className="text-sm text-text-muted">
          Nenhum dado é exibido aqui — nem real, nem de exemplo. Quando pronta, esta área vai oferecer:
        </p>
        <p className="text-sm text-text">{route.summary}</p>
      </div>
      <dl className="m-0 grid w-full grid-cols-1 gap-x-6 gap-y-3 border-t border-border pt-4 text-xs sm:grid-cols-3">
        <div>
          <dt className="text-text-muted">Endereço</dt>
          <dd className="m-0 font-semibold text-text">
            <code>{route.path}</code>
          </dd>
        </div>
        <div>
          <dt className="text-text-muted">Permissão necessária</dt>
          <dd className="m-0 font-semibold text-text">
            <code>{route.permission ?? 'usuário autenticado'}</code>
          </dd>
        </div>
        <div>
          <dt className="text-text-muted">Requisitos</dt>
          <dd className="m-0 font-semibold text-text">{route.refs.join(' · ')}</dd>
        </div>
      </dl>
    </Card>
  )
}
