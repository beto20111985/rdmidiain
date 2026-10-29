import * as React from 'react'
import { cn } from '@/lib/utils'
import { useFieldControl } from '@/components/ui/field'
import { inputBase } from '@/components/ui/input'

const Textarea = React.forwardRef<HTMLTextAreaElement, React.ComponentProps<'textarea'>>(
  ({ className, ...props }, ref) => {
    const field = useFieldControl(props)
    return <textarea ref={ref} className={cn(inputBase, 'min-h-24 resize-y', className)} {...props} {...field} />
  },
)
Textarea.displayName = 'Textarea'

export { Textarea }
