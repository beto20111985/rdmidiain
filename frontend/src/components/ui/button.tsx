import * as React from 'react'
import { Slot } from '@radix-ui/react-slot'
import { cva, type VariantProps } from 'class-variance-authority'
import { Loader2 } from 'lucide-react'
import { cn } from '@/lib/utils'

/**
 * Botão RD (DS §10): altura 44 px, padding 16 px, raio 8 px, peso 600.
 * Variantes do DS: primary (ação preenchida), secondary (transparente com borda perceptível),
 * tertiary (texto azul claro, sem caixa), destructive (fundo escuro, texto e borda `danger`).
 * `ghost` = botões de ícone/navegação.
 *
 * `loading`: desabilita de verdade (mesmo que a página passe `disabled={false}`), mantém a
 * largura (conteúdo fica invisível sob o spinner) e anuncia o progresso a leitores de tela.
 * `disabled`: não depende só de transparência — muda fundo, borda (tracejada) e cursor.
 * Botão só com ícone exige `aria-label` (verificado em dev).
 */
const buttonVariants = cva(
  [
    'relative inline-flex min-h-11 select-none items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-semibold',
    'transition-[background-color,border-color,color,box-shadow,transform] duration-fast ease-rd',
    'active:translate-y-px',
    '[&_svg]:pointer-events-none [&_svg]:size-[18px] [&_svg]:shrink-0',
    'disabled:cursor-not-allowed disabled:active:translate-y-0 aria-disabled:cursor-not-allowed',
  ],
  {
    variants: {
      variant: {
        primary:
          'bg-action px-4 text-action-foreground hover:bg-action-hover disabled:border disabled:border-dashed disabled:border-control-border disabled:bg-surface-hover disabled:text-text-muted',
        secondary:
          'border border-control-border bg-transparent px-4 text-text hover:bg-surface-hover disabled:border-dashed disabled:text-text-muted disabled:hover:bg-transparent',
        tertiary:
          'bg-transparent px-2 text-link underline-offset-4 hover:underline disabled:text-text-muted disabled:no-underline',
        destructive:
          'border border-danger bg-surface px-4 text-danger hover:bg-danger-soft disabled:border-dashed disabled:border-control-border disabled:text-text-muted disabled:hover:bg-surface',
        ghost:
          'bg-transparent px-3 text-text hover:bg-surface-hover disabled:text-text-muted disabled:hover:bg-transparent',
      },
      size: {
        default: '',
        icon: 'size-11 min-w-11 px-0',
        block: 'w-full',
      },
    },
    defaultVariants: {
      variant: 'primary',
      size: 'default',
    },
  },
)

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean
  loading?: boolean
  /** Texto anunciado durante o carregamento (padrão "Processando…"). */
  loadingText?: string
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  (
    { className, variant, size, asChild = false, loading = false, loadingText, disabled, children, type, onClick, ...props },
    ref,
  ) => {
    const isDisabled = Boolean(disabled) || loading

    if (import.meta.env.DEV && size === 'icon' && !props['aria-label'] && !props['aria-labelledby']) {
      console.warn('[Button] Botão só com ícone precisa de aria-label.')
    }

    // asChild (ex.: <Button asChild><Link/></Button>) — links não têm estado de carregamento.
    if (asChild) {
      return (
        <Slot
          ref={ref}
          className={cn(buttonVariants({ variant, size }), className)}
          aria-disabled={isDisabled || undefined}
          onClick={onClick}
          {...props}
        >
          {children}
        </Slot>
      )
    }

    return (
      <button
        ref={ref}
        type={type ?? 'button'}
        className={cn(buttonVariants({ variant, size }), className)}
        disabled={isDisabled}
        aria-busy={loading || undefined}
        data-loading={loading || undefined}
        onClick={isDisabled ? undefined : onClick}
        {...props}
      >
        {loading ? (
          <>
            <span className="invisible inline-flex items-center gap-2">{children}</span>
            <span className="absolute inset-0 inline-flex items-center justify-center" aria-hidden="true">
              <Loader2 className="animate-spin" />
            </span>
            <span className="sr-only" role="status">
              {loadingText ?? 'Processando…'}
            </span>
          </>
        ) : (
          children
        )}
      </button>
    )
  },
)
Button.displayName = 'Button'

// eslint-disable-next-line react-refresh/only-export-components
export { Button, buttonVariants }
