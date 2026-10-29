/** Lista de pares rótulo → valor (<dl>) para detalhes de registros financeiros/comerciais. */
import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

export interface DetailItem {
  label: string
  value: ReactNode
  /** Valor numérico (alinhado e com algarismos tabulares). */
  numeric?: boolean
  /** Ocupa a linha inteira (ex.: hashes, motivos). */
  wide?: boolean
}

export function DetailList({ items, className, columns = 2 }: { items: DetailItem[]; className?: string; columns?: 1 | 2 | 3 }) {
  return (
    <dl
      className={cn(
        'm-0 grid grid-cols-1 gap-x-6 gap-y-4',
        columns >= 2 && 'sm:grid-cols-2',
        columns === 3 && 'xl:grid-cols-3',
        className,
      )}
    >
      {items.map((it) => (
        <div key={it.label} className={cn('flex min-w-0 flex-col gap-1', it.wide && 'sm:col-span-2 xl:col-span-3')}>
          <dt className="text-xs font-semibold uppercase tracking-metric text-text-muted">{it.label}</dt>
          <dd className={cn('m-0 break-words text-sm text-text', it.numeric && 'tabular-nums')}>{it.value}</dd>
        </div>
      ))}
    </dl>
  )
}
