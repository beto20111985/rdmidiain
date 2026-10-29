/**
 * Progresso de um assistente em etapas (primeiro acesso, ativação do 2º Master): barra + lista
 * ordenada com `aria-current="step"` e texto de estado (nunca só cor/ícone).
 */
import { Check } from 'lucide-react'
import { Progress } from '@/components/ui/progress'
import { cn } from '@/lib/utils'

export type StepProgressState = 'done' | 'current' | 'pending'

export interface StepProgressItem {
  key: string
  label: string
  state: StepProgressState
}

export function StepProgress({ steps, label = 'Progresso da configuração' }: { steps: StepProgressItem[]; label?: string }) {
  const doneCount = steps.filter((s) => s.state === 'done').length
  const index = Math.max(0, steps.findIndex((s) => s.state === 'current'))
  const current = steps[index]
  return (
    <div className="flex flex-col gap-3">
      <p className="sr-only" role="status" aria-live="polite">
        {current ? `Etapa ${index + 1} de ${steps.length}: ${current.label}` : ''}
      </p>
      {steps.length > 1 ? (
        <>
          <Progress value={(doneCount / steps.length) * 100} aria-label={label} />
          <ol className="m-0 flex list-none flex-col gap-2 p-0 sm:flex-row sm:flex-wrap sm:gap-x-5" aria-label="Etapas">
            {steps.map((s, i) => (
              <li
                key={s.key}
                aria-current={s.state === 'current' ? 'step' : undefined}
                className={cn('flex min-h-8 items-center gap-2 text-sm', s.state === 'current' ? 'font-semibold text-text' : 'text-text-muted')}
              >
                <span
                  aria-hidden="true"
                  className={cn(
                    'inline-flex size-6 shrink-0 items-center justify-center rounded-full border text-xs font-semibold tabular-nums',
                    s.state === 'done' && 'border-success bg-success-soft text-success',
                    s.state === 'current' && 'border-action bg-action text-action-foreground',
                    s.state === 'pending' && 'border-control-border text-text-muted',
                  )}
                >
                  {s.state === 'done' ? <Check className="size-3.5" /> : i + 1}
                </span>
                <span>
                  {s.label}
                  <span className="sr-only">{s.state === 'done' ? ' (concluída)' : s.state === 'current' ? ' (etapa atual)' : ' (pendente)'}</span>
                </span>
              </li>
            ))}
          </ol>
        </>
      ) : null}
    </div>
  )
}
