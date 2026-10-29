import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

/**
 * Badge (DS §7/§10): 12/16 px peso 600, padding 4 × 8, fundo de baixa opacidade, texto legível,
 * caixa normal. Para status de domínio use <StatusBadge>, que fixa palavra + cor + ícone.
 */
const badgeVariants = cva(
  'inline-flex max-w-full items-center gap-1.5 whitespace-nowrap rounded-md px-2 py-1 text-badge [&_svg]:size-3.5 [&_svg]:shrink-0',
  {
    variants: {
      tone: {
        success: 'bg-success-soft text-success',
        info: 'bg-info-soft text-info',
        warning: 'bg-warning-soft text-warning',
        danger: 'bg-danger-soft text-danger',
        neutral: 'bg-neutral-soft text-neutral',
        outline: 'border border-control-border text-text',
      },
    },
    defaultVariants: { tone: 'neutral' },
  },
)

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement>, VariantProps<typeof badgeVariants> {}

function Badge({ className, tone, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ tone }), className)} {...props} />
}

// eslint-disable-next-line react-refresh/only-export-components
export { Badge, badgeVariants }
