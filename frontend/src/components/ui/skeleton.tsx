import * as React from 'react'
import { cn } from '@/lib/utils'

/** Esqueleto de carregamento (DS §10 "Skeleton durante carga inicial"); pulso desligado com reduced-motion. */
function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div aria-hidden="true" className={cn('animate-pulse rounded-md bg-surface-hover', className)} {...props} />
}

export { Skeleton }
