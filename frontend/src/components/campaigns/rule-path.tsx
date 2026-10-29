/**
 * Explicação de uma decisão do motor de conflitos (anúncio × estabelecimento): resultado,
 * motivo e o CAMINHO de regras avaliado (passo a passo), como o backend registrou.
 */
import { Ban, CheckCircle2, MinusCircle } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { conflictReasonLabel } from '@/pages/campaigns/lib/labels'
import { shortId } from '@/pages/campaigns/lib/format'
import type { ConflictDecision, ConflictStep } from '@/pages/campaigns/lib/types'

function ResultBadge({ result }: { result?: string }) {
  if (result === 'BLOCK')
    return (
      <Badge tone="danger">
        <Ban aria-hidden="true" />
        Bloqueia
      </Badge>
    )
  if (result === 'ALLOW')
    return (
      <Badge tone="success">
        <CheckCircle2 aria-hidden="true" />
        Libera
      </Badge>
    )
  return (
    <Badge tone="neutral">
      <MinusCircle aria-hidden="true" />
      Não se aplica
    </Badge>
  )
}

function Steps({ steps, nested = false }: { steps: ConflictStep[]; nested?: boolean }) {
  return (
    <ol className={`m-0 flex list-decimal flex-col gap-2 ${nested ? 'pl-5' : 'pl-5'}`}>
      {steps.map((s, i) => (
        <li key={i} className="text-sm">
          {s.creative_id ? (
            <div className="flex flex-col gap-1">
              <span className="flex flex-wrap items-center gap-2">
                Anúncio {shortId(s.creative_id)} <ResultBadge result={s.result} /> {conflictReasonLabel(s.reason_code)}
              </span>
              {s.rule_path?.length ? <Steps steps={s.rule_path} nested /> : null}
            </div>
          ) : (
            <span className="flex flex-wrap items-center gap-2">
              <span>{conflictReasonLabel(s.check)}</span>
              <ResultBadge result={s.result} />
              {s.note ? <span className="text-text-muted">— {s.note}</span> : null}
              {s.detail ? (
                <code className="max-w-full truncate rounded bg-surface-hover px-1.5 py-0.5 text-xs">{JSON.stringify(s.detail)}</code>
              ) : null}
            </span>
          )}
        </li>
      ))}
    </ol>
  )
}

export function RulePath({ decision }: { decision: Pick<ConflictDecision, 'result' | 'reason_code' | 'rule_path'> & Partial<ConflictDecision> }) {
  return (
    <div className="flex flex-col gap-3" data-testid="conflict-decision">
      <p className="m-0 flex flex-wrap items-center gap-2 text-sm">
        <span className="font-semibold">Decisão:</span>
        <ResultBadge result={decision.result} />
        <span>{conflictReasonLabel(decision.reason_code)}</span>
        {decision.engine_version ? <span className="text-xs text-text-muted">motor {decision.engine_version}</span> : null}
      </p>
      {decision.rule_path?.length ? (
        <div className="flex flex-col gap-1">
          <p className="m-0 text-xs font-semibold text-text-muted">Caminho das regras avaliadas</p>
          <Steps steps={decision.rule_path} />
        </div>
      ) : null}
    </div>
  )
}
