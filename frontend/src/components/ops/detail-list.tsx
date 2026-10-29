/** Lista de definição (rótulo: valor) para detalhes — sem dado → "—". */
import * as React from 'react'
import { cn } from '@/lib/utils'
import { EMPTY_VALUE } from '@/lib/format'

export interface DetailItem {
  label: string
  value: React.ReactNode
  /** Ocupa a linha inteira. */
  wide?: boolean
  numeric?: boolean
}

export function DetailList({ items, className, columns = 2 }: { items: DetailItem[]; className?: string; columns?: 1 | 2 | 3 }) {
  return (
    <dl
      className={cn(
        'm-0 grid grid-cols-1 gap-x-6 gap-y-4 text-sm',
        columns >= 2 && 'sm:grid-cols-2',
        columns === 3 && 'xl:grid-cols-3',
        className,
      )}
    >
      {items.map((it) => (
        <div key={it.label} className={cn('flex min-w-0 flex-col gap-0.5', it.wide && 'sm:col-span-full')}>
          <dt className="text-xs text-text-muted">{it.label}</dt>
          <dd className={cn('m-0 min-w-0 break-words text-text', it.numeric && 'tabular-nums')}>
            {it.value === null || it.value === undefined || it.value === '' ? EMPTY_VALUE : it.value}
          </dd>
        </div>
      ))}
    </dl>
  )
}
