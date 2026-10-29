/**
 * Field (composição shadcn adaptada ao DS RD §10 "Inputs e filtros").
 *
 * <Field invalid={!!errors.email}>
 *   <FieldLabel required>E-mail</FieldLabel>
 *   <Input type="email" {...register('email')} />
 *   <FieldDescription>Usado para entrar no painel.</FieldDescription>
 *   <FieldError>{errors.email?.message}</FieldError>
 * </Field>
 *
 * - Label persistente acima (placeholder nunca substitui label).
 * - O controle recebe automaticamente `id`, `aria-describedby` (descrição + erro) e
 *   `aria-invalid` pelo contexto — sem ids manuais.
 * - Erro junto ao campo, com ícone e texto (nunca só cor).
 */
import * as React from 'react'
import { AlertCircle } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Label } from '@/components/ui/label'

interface FieldContextValue {
  controlId: string
  descriptionId: string
  errorId: string
  hasDescription: boolean
  hasError: boolean
  invalid: boolean
  disabled: boolean
  required: boolean
  registerDescription: (present: boolean) => void
  registerError: (present: boolean) => void
}

const FieldContext = React.createContext<FieldContextValue | null>(null)

/** Props ARIA para qualquer controle dentro de <Field>. Seguro fora de Field (retorna {}). */
// eslint-disable-next-line react-refresh/only-export-components
export function useFieldControl(props: {
  id?: string
  'aria-describedby'?: string
  'aria-invalid'?: React.AriaAttributes['aria-invalid']
  disabled?: boolean
  required?: boolean
}) {
  const ctx = React.useContext(FieldContext)
  if (!ctx) return {}
  const describedBy = [
    props['aria-describedby'],
    ctx.hasDescription ? ctx.descriptionId : null,
    ctx.hasError ? ctx.errorId : null,
  ]
    .filter(Boolean)
    .join(' ')
  return {
    id: props.id ?? ctx.controlId,
    'aria-describedby': describedBy || undefined,
    'aria-invalid': props['aria-invalid'] ?? (ctx.invalid || undefined),
    disabled: props.disabled ?? (ctx.disabled || undefined),
    required: props.required ?? (ctx.required || undefined),
  }
}

// eslint-disable-next-line react-refresh/only-export-components
export function useFieldContext() {
  return React.useContext(FieldContext)
}

interface FieldProps extends React.HTMLAttributes<HTMLDivElement> {
  invalid?: boolean
  disabled?: boolean
  required?: boolean
  /** id do controle (opcional; gerado automaticamente). */
  controlId?: string
  orientation?: 'vertical' | 'horizontal'
}

function Field({
  className,
  invalid = false,
  disabled = false,
  required = false,
  controlId,
  orientation = 'vertical',
  children,
  ...props
}: FieldProps) {
  const autoId = React.useId()
  const [hasDescription, setHasDescription] = React.useState(false)
  const [hasError, setHasError] = React.useState(false)
  const ctx = React.useMemo<FieldContextValue>(
    () => ({
      controlId: controlId ?? `f${autoId.replace(/:/g, '')}`,
      descriptionId: `f${autoId.replace(/:/g, '')}-desc`,
      errorId: `f${autoId.replace(/:/g, '')}-err`,
      hasDescription,
      hasError,
      invalid: invalid || hasError,
      disabled,
      required,
      registerDescription: setHasDescription,
      registerError: setHasError,
    }),
    [autoId, controlId, hasDescription, hasError, invalid, disabled, required],
  )
  return (
    <FieldContext.Provider value={ctx}>
      <div
        role="group"
        data-slot="field"
        data-invalid={ctx.invalid || undefined}
        data-disabled={disabled || undefined}
        data-orientation={orientation}
        className={cn(
          'flex w-full gap-2',
          orientation === 'vertical' ? 'flex-col' : 'flex-row flex-wrap items-center gap-x-3',
          className,
        )}
        {...props}
      >
        {children}
      </div>
    </FieldContext.Provider>
  )
}

function FieldLabel({
  className,
  children,
  required,
  ...props
}: React.ComponentPropsWithoutRef<typeof Label> & { required?: boolean }) {
  const ctx = React.useContext(FieldContext)
  const isRequired = required ?? ctx?.required
  return (
    <Label
      data-slot="field-label"
      htmlFor={props.htmlFor ?? ctx?.controlId}
      className={cn('w-fit', ctx?.disabled && 'text-text-muted', className)}
      {...props}
    >
      {children}
      {isRequired ? (
        <span className="ml-1 text-text-muted" aria-hidden="true">
          *
        </span>
      ) : null}
      {isRequired ? <span className="sr-only"> (obrigatório)</span> : null}
    </Label>
  )
}

function FieldDescription({ className, ...props }: React.HTMLAttributes<HTMLParagraphElement>) {
  const ctx = React.useContext(FieldContext)
  const register = ctx?.registerDescription
  React.useEffect(() => {
    register?.(true)
    return () => register?.(false)
  }, [register])
  return (
    <p
      id={ctx?.descriptionId}
      data-slot="field-description"
      className={cn('text-xs text-text-muted', className)}
      {...props}
    />
  )
}

function FieldError({ className, children, ...props }: React.HTMLAttributes<HTMLParagraphElement>) {
  const ctx = React.useContext(FieldContext)
  const register = ctx?.registerError
  const present = children !== undefined && children !== null && children !== false && children !== ''
  React.useEffect(() => {
    register?.(present)
    return () => register?.(false)
  }, [register, present])
  if (!present) return null
  return (
    <p
      id={ctx?.errorId}
      data-slot="field-error"
      className={cn('flex items-start gap-1.5 text-xs font-medium text-danger', className)}
      {...props}
    >
      <AlertCircle aria-hidden="true" className="mt-px size-4 shrink-0" />
      <span>{children}</span>
    </p>
  )
}

function FieldGroup({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div data-slot="field-group" className={cn('flex w-full flex-col gap-5', className)} {...props} />
}

function FieldSet({ className, ...props }: React.FieldsetHTMLAttributes<HTMLFieldSetElement>) {
  return <fieldset data-slot="field-set" className={cn('m-0 flex min-w-0 flex-col gap-3 border-0 p-0', className)} {...props} />
}

function FieldLegend({ className, ...props }: React.HTMLAttributes<HTMLLegendElement>) {
  return <legend data-slot="field-legend" className={cn('mb-1 p-0 text-sm font-semibold text-text', className)} {...props} />
}

export { Field, FieldLabel, FieldDescription, FieldError, FieldGroup, FieldSet, FieldLegend }
