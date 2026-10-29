import * as React from 'react'
import { cn } from '@/lib/utils'

/** Primitivas de estado vazio (shadcn Empty). Para o padrão RD completo use <EmptyState>. */
function Empty({ className, ...props }: React.ComponentProps<'div'>) {
  return (
    <div
      data-slot="empty"
      className={cn('flex min-w-0 flex-1 flex-col items-center justify-center gap-6 rounded-lg p-6 text-center md:p-12', className)}
      {...props}
    />
  )
}

function EmptyHeader({ className, ...props }: React.ComponentProps<'div'>) {
  return <div data-slot="empty-header" className={cn('flex max-w-md flex-col items-center gap-2', className)} {...props} />
}

function EmptyMedia({ className, ...props }: React.ComponentProps<'div'>) {
  return (
    <div
      data-slot="empty-icon"
      aria-hidden="true"
      className={cn(
        'mb-2 flex size-12 shrink-0 items-center justify-center rounded-lg border border-border bg-surface-hover text-link [&_svg]:size-6',
        className,
      )}
      {...props}
    />
  )
}

function EmptyTitle({ className, ...props }: React.ComponentProps<'h2'>) {
  return <h2 data-slot="empty-title" className={cn('text-title-section text-text', className)} {...props} />
}

function EmptyDescription({ className, ...props }: React.ComponentProps<'p'>) {
  return <p data-slot="empty-description" className={cn('text-sm text-text-muted', className)} {...props} />
}

function EmptyContent({ className, ...props }: React.ComponentProps<'div'>) {
  return <div data-slot="empty-content" className={cn('flex flex-wrap items-center justify-center gap-3', className)} {...props} />
}

export { Empty, EmptyHeader, EmptyTitle, EmptyDescription, EmptyContent, EmptyMedia }
