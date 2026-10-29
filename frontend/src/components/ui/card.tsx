import * as React from 'react'
import { cn } from '@/lib/utils'

/**
 * Cartão RD (DS §6/§16): superfície `surface`, contorno 1 px, raio 16 px, sombra leve,
 * padding 24 px (16 px no mobile). Nunca aninhe cartões.
 */
const Card = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(({ className, ...props }, ref) => (
  <div
    ref={ref}
    data-slot="card"
    className={cn('flex min-w-0 flex-col gap-4 rounded-xl border border-border bg-surface p-6 text-text shadow-card max-md:p-4', className)}
    {...props}
  />
))
Card.displayName = 'Card'

const CardHeader = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(({ className, ...props }, ref) => (
  <div ref={ref} className={cn('flex flex-wrap items-start justify-between gap-x-4 gap-y-2', className)} {...props} />
))
CardHeader.displayName = 'CardHeader'

/** Título de cartão: 14/20 peso 600 (DS §7). Use `as` para o nível de heading correto (padrão h2). */
const CardTitle = React.forwardRef<HTMLHeadingElement, React.HTMLAttributes<HTMLHeadingElement> & { as?: 'h2' | 'h3' | 'h4' }>(
  ({ className, as: Tag = 'h2', ...props }, ref) => <Tag ref={ref} className={cn('text-title-card text-text', className)} {...props} />,
)
CardTitle.displayName = 'CardTitle'

const CardDescription = React.forwardRef<HTMLParagraphElement, React.HTMLAttributes<HTMLParagraphElement>>(
  ({ className, ...props }, ref) => <p ref={ref} className={cn('text-xs text-text-muted', className)} {...props} />,
)
CardDescription.displayName = 'CardDescription'

const CardContent = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(({ className, ...props }, ref) => (
  <div ref={ref} className={cn('min-w-0', className)} {...props} />
))
CardContent.displayName = 'CardContent'

const CardFooter = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(({ className, ...props }, ref) => (
  <div ref={ref} className={cn('flex flex-wrap items-center gap-3', className)} {...props} />
))
CardFooter.displayName = 'CardFooter'

export { Card, CardHeader, CardFooter, CardTitle, CardDescription, CardContent }
