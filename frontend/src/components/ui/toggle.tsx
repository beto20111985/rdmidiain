import * as React from 'react'
import * as TogglePrimitive from '@radix-ui/react-toggle'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

/** Alternador (ex.: período Hoje/7 dias/30 dias). Ativo = fundo de ação + texto, não só cor de borda. */
const toggleVariants = cva(
  'inline-flex min-h-11 min-w-11 items-center justify-center gap-2 rounded-md px-3 text-sm font-semibold text-text-muted transition-colors duration-fast ease-rd hover:bg-surface-hover hover:text-text disabled:cursor-not-allowed disabled:text-text-muted/70 data-[state=on]:bg-action data-[state=on]:text-action-foreground [&_svg]:pointer-events-none [&_svg]:size-[18px] [&_svg]:shrink-0',
  {
    variants: {
      variant: {
        default: 'bg-transparent',
        outline: 'border border-control-border bg-transparent data-[state=on]:border-action',
      },
    },
    defaultVariants: { variant: 'default' },
  },
)

const Toggle = React.forwardRef<
  React.ElementRef<typeof TogglePrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof TogglePrimitive.Root> & VariantProps<typeof toggleVariants>
>(({ className, variant, ...props }, ref) => (
  <TogglePrimitive.Root ref={ref} className={cn(toggleVariants({ variant }), className)} {...props} />
))
Toggle.displayName = TogglePrimitive.Root.displayName

// eslint-disable-next-line react-refresh/only-export-components
export { Toggle, toggleVariants }
