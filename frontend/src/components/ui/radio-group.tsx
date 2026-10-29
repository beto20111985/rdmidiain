import * as React from 'react'
import * as RadioGroupPrimitive from '@radix-ui/react-radio-group'
import { cn } from '@/lib/utils'
import { useFieldControl } from '@/components/ui/field'

const RadioGroup = React.forwardRef<
  React.ElementRef<typeof RadioGroupPrimitive.Root>,
  React.ComponentPropsWithoutRef<typeof RadioGroupPrimitive.Root>
>(({ className, ...props }, ref) => {
  const field = useFieldControl(props)
  const { id: _id, ...aria } = field as { id?: string }
  void _id
  return <RadioGroupPrimitive.Root ref={ref} className={cn('grid gap-1', className)} {...props} {...aria} />
})
RadioGroup.displayName = RadioGroupPrimitive.Root.displayName

const RadioGroupItem = React.forwardRef<
  React.ElementRef<typeof RadioGroupPrimitive.Item>,
  React.ComponentPropsWithoutRef<typeof RadioGroupPrimitive.Item>
>(({ className, ...props }, ref) => (
  <RadioGroupPrimitive.Item
    ref={ref}
    className={cn(
      'peer relative grid aspect-square size-5 shrink-0 place-content-center rounded-full border-2 before:absolute before:-inset-3 before:content-[""] border-control-border bg-field transition-colors duration-fast ease-rd disabled:cursor-not-allowed disabled:border-dashed data-[state=checked]:border-action',
      className,
    )}
    {...props}
  >
    <RadioGroupPrimitive.Indicator className="block size-2.5 rounded-full bg-action" />
  </RadioGroupPrimitive.Item>
))
RadioGroupItem.displayName = RadioGroupPrimitive.Item.displayName

export { RadioGroup, RadioGroupItem }
