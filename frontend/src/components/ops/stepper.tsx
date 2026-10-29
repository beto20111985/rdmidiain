/** Lista de etapas de um wizard (navegável por teclado; etapa atual com aria-current="step"). */
import { CheckCircle2, Circle } from 'lucide-react'
import { cn } from '@/lib/utils'

export function Stepper({
  steps,
  current,
  onSelect,
  completed,
  label = 'Etapas do cadastro',
}: {
  steps: Array<{ key: string; label: string }>
  current: string
  onSelect: (key: string) => void
  completed: (key: string) => boolean
  label?: string
}) {
  return (
    <nav aria-label={label}>
      <ol className="m-0 flex list-none gap-1 overflow-x-auto p-0 md:flex-col">
        {steps.map((s, i) => {
          const done = completed(s.key)
          const active = s.key === current
          return (
            <li key={s.key} className="shrink-0">
              <button
                type="button"
                onClick={() => onSelect(s.key)}
                aria-current={active ? 'step' : undefined}
                className={cn(
                  'flex min-h-11 w-full items-center gap-2 rounded-md px-3 text-left text-sm transition-colors duration-fast ease-rd hover:bg-surface-hover',
                  active ? 'bg-surface-hover font-semibold text-text' : 'text-text-muted',
                )}
              >
                {done ? <CheckCircle2 aria-hidden="true" className="size-4 shrink-0 text-success" /> : <Circle aria-hidden="true" className="size-4 shrink-0" />}
                <span className="whitespace-nowrap">
                  {i + 1}. {s.label}
                </span>
                {done ? <span className="sr-only">(concluída)</span> : null}
              </button>
            </li>
          )
        })}
      </ol>
    </nav>
  )
}
