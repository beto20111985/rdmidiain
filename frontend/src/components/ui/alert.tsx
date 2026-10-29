import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

/**
 * Aviso em linha (callout). Erros que exigem correção ficam na tela (DS §10) — use Alert,
 * não toast. Sempre com texto; a cor é reforço. `role` padrão: "status" (informativo) ou
 * "alert" (variante danger).
 */
const alertVariants = cva(
  'relative flex w-full gap-3 rounded-lg border px-4 py-3 text-sm [&>svg]:mt-0.5 [&>svg]:size-5 [&>svg]:shrink-0',
  {
    variants: {
      variant: {
        info: 'border-info/40 bg-info-soft text-text [&>svg]:text-info',
        success: 'border-success/40 bg-success-soft text-text [&>svg]:text-success',
        warning: 'border-warning/50 bg-warning-soft text-text [&>svg]:text-warning',
        danger: 'border-danger/50 bg-danger-soft text-text [&>svg]:text-danger',
        neutral: 'border-border bg-surface text-text [&>svg]:text-text-muted',
      },
    },
    defaultVariants: { variant: 'info' },
  },
)

const Alert = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement> & VariantProps<typeof alertVariants>>(
  ({ className, variant, role, ...props }, ref) => (
    <div
      ref={ref}
      role={role ?? (variant === 'danger' ? 'alert' : 'status')}
      className={cn(alertVariants({ variant }), className)}
      {...props}
    />
  ),
)
Alert.displayName = 'Alert'

const AlertTitle = React.forwardRef<HTMLParagraphElement, React.HTMLAttributes<HTMLParagraphElement>>(
  ({ className, ...props }, ref) => <p ref={ref} className={cn('font-semibold text-text', className)} {...props} />,
)
AlertTitle.displayName = 'AlertTitle'

const AlertDescription = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => <div ref={ref} className={cn('text-sm text-text-muted', className)} {...props} />,
)
AlertDescription.displayName = 'AlertDescription'

export { Alert, AlertTitle, AlertDescription }
