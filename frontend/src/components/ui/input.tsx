import * as React from 'react'
import { cn } from '@/lib/utils'
import { useFieldControl } from '@/components/ui/field'

/**
 * Input (DS §10): altura 44 px, fundo `field`, borda `control-border` (≥ 3:1), texto claro,
 * erro com borda `danger`, foco 2 px `focus` + offset 2 px (global :focus-visible),
 * 16 px no mobile (globals.css). Dentro de <Field> recebe id/aria automaticamente.
 */
export const inputBase =
  'flex min-h-11 w-full rounded-md border border-control-border bg-field px-3 py-2 text-sm text-text transition-colors duration-fast ease-rd placeholder:text-text-muted hover:border-text-muted disabled:cursor-not-allowed disabled:border-dashed disabled:bg-surface-hover disabled:text-text-muted aria-[invalid=true]:border-danger aria-[invalid=true]:border-2'

const Input = React.forwardRef<HTMLInputElement, React.ComponentProps<'input'>>(
  ({ className, type = 'text', ...props }, ref) => {
    const field = useFieldControl(props)
    return (
      <input
        type={type}
        ref={ref}
        className={cn(
          inputBase,
          'file:mr-3 file:border-0 file:bg-transparent file:text-sm file:font-semibold file:text-text',
          className,
        )}
        {...props}
        {...field}
      />
    )
  },
)
Input.displayName = 'Input'

export { Input }
