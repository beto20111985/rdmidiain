import { Toaster as Sonner } from 'sonner'
import { useThemeStore } from '@/stores/themeStore'

type ToasterProps = React.ComponentProps<typeof Sonner>

/**
 * Toasts (sonner) — só para confirmações curtas (DS §10). Erros que exigem correção
 * ficam na tela (Alert/FieldError). Segue o tema RD e anuncia via região live do sonner.
 * Uso: import { toast } from 'sonner'; toast.success('Campanha salva.')
 */
const Toaster = (props: ToasterProps) => {
  const theme = useThemeStore((s) => s.resolved)
  return (
    <Sonner
      theme={theme}
      position="bottom-right"
      closeButton
      containerAriaLabel="Notificações"
      style={{ zIndex: 'var(--z-toast)' } as React.CSSProperties}
      toastOptions={{
        classNames: {
          toast:
            'group rounded-lg border border-border bg-surface-raised text-text shadow-overlay font-sans text-sm',
          title: 'font-semibold text-text',
          description: 'text-text-muted',
          actionButton: 'bg-action text-action-foreground font-semibold',
          cancelButton: 'bg-surface-hover text-text',
          closeButton: 'border-border bg-surface-raised text-text-muted hover:text-text',
          success: '[&_[data-icon]]:text-success',
          error: '[&_[data-icon]]:text-danger',
          warning: '[&_[data-icon]]:text-warning',
          info: '[&_[data-icon]]:text-info',
        },
      }}
      {...props}
    />
  )
}

export { Toaster }
