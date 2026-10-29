import * as React from 'react'
import * as SwitchPrimitives from '@radix-ui/react-switch'
import { cn } from '@/lib/utils'
import { useFieldControl } from '@/components/ui/field'

/** Interruptor para configurações (estado também anunciado por role="switch"). */
const Switch = React.forwardRef<
  React.ElementRef<typeof SwitchPrimitives.Root>,
  React.ComponentPropsWithoutRef<typeof SwitchPrimitives.Root>
>(({ className, ...props }, ref) => {
  const field = useFieldControl(props)
  return (
    <SwitchPrimitives.Root
      ref={ref}
      className={cn(
        'peer relative inline-flex h-6 w-11 shrink-0 cursor-pointer items-center rounded-full border-2 before:absolute before:-inset-y-2.5 before:inset-x-0 before:content-[""] transition-colors duration-fast ease-rd disabled:cursor-not-allowed disabled:border-dashed disabled:opacity-70 data-[state=checked]:border-action data-[state=checked]:bg-action data-[state=unchecked]:border-control-border data-[state=unchecked]:bg-field',
        className,
      )}
      {...props}
      {...field}
    >
      <SwitchPrimitives.Thumb
        className={cn(
          'pointer-events-none block size-4 rounded-full shadow transition-transform duration-fast ease-rd data-[state=checked]:translate-x-[22px] data-[state=checked]:bg-action-foreground data-[state=unchecked]:translate-x-[2px] data-[state=unchecked]:bg-text-muted',
        )}
      />
    </SwitchPrimitives.Root>
  )
})
Switch.displayName = SwitchPrimitives.Root.displayName

export { Switch }
