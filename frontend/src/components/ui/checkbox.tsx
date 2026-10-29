import * as React from 'react'
import * as CheckboxPrimitive from '@radix-ui/react-checkbox'
import { Check, Minus } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useFieldControl } from '@/components/ui/field'

/** Caixa de 20 px (borda ≥ 3:1); use com <Field orientation="horizontal"> para alvo de 44 px com o rótulo. */
const Checkbox = React.forwardRef<
  React.ElementRef<typeof CheckboxPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof CheckboxPrimitive.Root>
>(({ className, ...props }, ref) => {
  const field = useFieldControl(props)
  return (
    <CheckboxPrimitive.Root
      ref={ref}
      className={cn(
        'peer relative grid size-5 shrink-0 place-content-center rounded-[5px] border-2 before:absolute before:-inset-3 before:content-[""] border-control-border bg-field transition-colors duration-fast ease-rd disabled:cursor-not-allowed disabled:border-dashed disabled:bg-surface-hover aria-[invalid=true]:border-danger data-[state=checked]:border-action data-[state=checked]:bg-action data-[state=checked]:text-action-foreground data-[state=indeterminate]:border-action data-[state=indeterminate]:bg-action data-[state=indeterminate]:text-action-foreground',
        className,
      )}
      {...props}
      {...field}
    >
      <CheckboxPrimitive.Indicator className="grid place-content-center text-current">
        {props.checked === 'indeterminate' ? (
          <Minus className="size-4" strokeWidth={3} aria-hidden="true" />
        ) : (
          <Check className="size-4" strokeWidth={3} aria-hidden="true" />
        )}
      </CheckboxPrimitive.Indicator>
    </CheckboxPrimitive.Root>
  )
})
Checkbox.displayName = CheckboxPrimitive.Root.displayName

export { Checkbox }
