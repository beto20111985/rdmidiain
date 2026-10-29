import * as React from 'react'
import { Loader2 } from 'lucide-react'
import { cn } from '@/lib/utils'

/** Indicador de progresso com nome acessível (padrão "Carregando…"). */
function Spinner({ className, label = 'Carregando…', ...props }: React.ComponentProps<'svg'> & { label?: string }) {
  return <Loader2 role="status" aria-label={label} className={cn('size-5 animate-spin text-link', className)} {...props} />
}

export { Spinner }
