import * as React from 'react'
import * as SelectPrimitive from '@radix-ui/react-select'
import { Check, ChevronDown, ChevronUp } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useFieldControl } from '@/components/ui/field'

type SelectRootProps = React.ComponentPropsWithoutRef<typeof SelectPrimitive.Root>

export interface SelectProps<T extends string = string> extends Omit<SelectRootProps, 'value' | 'defaultValue' | 'onValueChange'> {
  value?: T
  defaultValue?: T
  onValueChange?: (value: T) => void
}

/**
 * Select (Radix) com o visual dos campos RD; dentro de <Field> recebe id/aria automaticamente.
 *
 * Proteção ÚNICA da plataforma (consolidada das cópias locais de OPS, CAMP, ADM e FIN): dentro de
 * `<form>` o Radix sincroniza um `<select>` nativo oculto e dispara `onValueChange('')` quando o
 * valor controlado ainda não tem `<SelectItem>` registrado (valor pré-preenchido antes de as
 * opções chegarem). Repassar `''` apagaria a escolha e, com efeitos que restauram o valor, gerava
 * laço síncrono de renderização (travou a suíte). Aqui `''` nunca é repassado — o Radix proíbe `''`
 * como valor de item, então nunca é uma escolha real — e o valor igual ao atual também não.
 * Genérico: `onValueChange` recebe o mesmo tipo de `value` (ex.: união de status), sem casts.
 */
function Select<T extends string = string>({ value, defaultValue, onValueChange, ...props }: SelectProps<T>) {
  const handleValueChange = React.useCallback(
    (next: string) => {
      if (next === '' || (value !== undefined && next === value)) return
      onValueChange?.(next as T)
    },
    [onValueChange, value],
  )
  return <SelectPrimitive.Root value={value} defaultValue={defaultValue} onValueChange={handleValueChange} {...props} />
}
const SelectGroup = SelectPrimitive.Group
const SelectValue = SelectPrimitive.Value

const SelectTrigger = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Trigger>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Trigger>
>(({ className, children, ...props }, ref) => {
  const field = useFieldControl(props)
  return (
    <SelectPrimitive.Trigger
      ref={ref}
      className={cn(
        'flex min-h-11 w-full items-center justify-between gap-2 whitespace-nowrap rounded-md border border-control-border bg-field px-3 py-2 text-left text-sm text-text transition-colors duration-fast ease-rd hover:border-text-muted data-[placeholder]:text-text-muted disabled:cursor-not-allowed disabled:border-dashed disabled:bg-surface-hover disabled:text-text-muted aria-[invalid=true]:border-2 aria-[invalid=true]:border-danger [&>span]:line-clamp-1',
        className,
      )}
      {...props}
      {...field}
    >
      {children}
      <SelectPrimitive.Icon asChild>
        <ChevronDown className="size-[18px] shrink-0 text-text-muted" aria-hidden="true" />
      </SelectPrimitive.Icon>
    </SelectPrimitive.Trigger>
  )
})
SelectTrigger.displayName = SelectPrimitive.Trigger.displayName

const SelectScrollUpButton = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.ScrollUpButton>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.ScrollUpButton>
>(({ className, ...props }, ref) => (
  <SelectPrimitive.ScrollUpButton ref={ref} className={cn('flex cursor-default items-center justify-center py-1', className)} {...props}>
    <ChevronUp className="size-4" aria-hidden="true" />
  </SelectPrimitive.ScrollUpButton>
))
SelectScrollUpButton.displayName = SelectPrimitive.ScrollUpButton.displayName

const SelectScrollDownButton = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.ScrollDownButton>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.ScrollDownButton>
>(({ className, ...props }, ref) => (
  <SelectPrimitive.ScrollDownButton ref={ref} className={cn('flex cursor-default items-center justify-center py-1', className)} {...props}>
    <ChevronDown className="size-4" aria-hidden="true" />
  </SelectPrimitive.ScrollDownButton>
))
SelectScrollDownButton.displayName = SelectPrimitive.ScrollDownButton.displayName

const SelectContent = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Content>
>(({ className, children, position = 'popper', ...props }, ref) => (
  <SelectPrimitive.Portal>
    <SelectPrimitive.Content
      ref={ref}
      className={cn(
        'relative z-popover max-h-[--radix-select-content-available-height] min-w-[8rem] overflow-y-auto overflow-x-hidden rounded-lg border border-border bg-popover text-popover-foreground shadow-overlay',
        'duration-fast ease-rd data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 origin-[--radix-select-content-transform-origin]',
        position === 'popper' && 'data-[side=bottom]:translate-y-1 data-[side=top]:-translate-y-1',
        className,
      )}
      position={position}
      {...props}
    >
      <SelectScrollUpButton />
      <SelectPrimitive.Viewport
        className={cn(
          'p-1',
          position === 'popper' && 'h-[var(--radix-select-trigger-height)] w-full min-w-[var(--radix-select-trigger-width)]',
        )}
      >
        {children}
      </SelectPrimitive.Viewport>
      <SelectScrollDownButton />
    </SelectPrimitive.Content>
  </SelectPrimitive.Portal>
))
SelectContent.displayName = SelectPrimitive.Content.displayName

const SelectLabel = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Label>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Label>
>(({ className, ...props }, ref) => (
  <SelectPrimitive.Label ref={ref} className={cn('px-3 py-1.5 text-xs font-semibold text-text-muted', className)} {...props} />
))
SelectLabel.displayName = SelectPrimitive.Label.displayName

const SelectItem = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Item>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Item>
>(({ className, children, ...props }, ref) => (
  <SelectPrimitive.Item
    ref={ref}
    className={cn(
      'relative flex min-h-11 w-full cursor-default select-none items-center rounded-md py-2 pl-3 pr-9 text-sm text-text outline-none focus:bg-surface-hover data-[disabled]:pointer-events-none data-[state=checked]:font-semibold data-[disabled]:text-text-muted',
      className,
    )}
    {...props}
  >
    <span className="absolute right-3 flex size-4 items-center justify-center">
      <SelectPrimitive.ItemIndicator>
        <Check className="size-4 text-link" aria-hidden="true" />
      </SelectPrimitive.ItemIndicator>
    </span>
    <SelectPrimitive.ItemText>{children}</SelectPrimitive.ItemText>
  </SelectPrimitive.Item>
))
SelectItem.displayName = SelectPrimitive.Item.displayName

const SelectSeparator = React.forwardRef<
  React.ElementRef<typeof SelectPrimitive.Separator>,
  React.ComponentPropsWithoutRef<typeof SelectPrimitive.Separator>
>(({ className, ...props }, ref) => (
  <SelectPrimitive.Separator ref={ref} className={cn('-mx-1 my-1 h-px bg-border', className)} {...props} />
))
SelectSeparator.displayName = SelectPrimitive.Separator.displayName

export {
  Select,
  SelectGroup,
  SelectValue,
  SelectTrigger,
  SelectContent,
  SelectLabel,
  SelectItem,
  SelectSeparator,
  SelectScrollUpButton,
  SelectScrollDownButton,
}
