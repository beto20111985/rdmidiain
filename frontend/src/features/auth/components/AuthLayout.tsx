import * as React from 'react'
import { LogoHorizontal } from '@/components/layout/logo'
import { cn } from '@/lib/utils'
import { pageTitle, useBrandName } from '@/components/admin/branding/brand'

/**
 * Moldura das telas públicas de autenticação: marca no topo, um único <h1>, cartão
 * centralizado (até 480 px), assinatura da marca abaixo (fora da logo — DS §3/§4).
 */
export function AuthLayout({
  title,
  description,
  children,
  footer,
  wide = false,
}: {
  title: string
  description?: React.ReactNode
  children: React.ReactNode
  footer?: React.ReactNode
  wide?: boolean
}) {
  const brandName = useBrandName()
  React.useEffect(() => {
    document.title = pageTitle(title, brandName)
  }, [title, brandName])
  return (
    <div className="flex min-h-dvh flex-col items-center justify-center gap-8 px-4 py-10">
      <a href="#conteudo" className="rd-skip-link">
        Pular para o conteúdo
      </a>
      <header>
        <LogoHorizontal />
      </header>
      <main id="conteudo" tabIndex={-1} className={cn('w-full outline-none', wide ? 'max-w-modal-md' : 'max-w-modal-sm')}>
        <div className="flex flex-col gap-6 rounded-2xl border border-border bg-surface p-6 shadow-card md:p-8">
          <div className="flex flex-col gap-2">
            <h1 className="m-0 text-title-page-sm text-text md:text-title-page">{title}</h1>
            {description ? <p className="m-0 text-sm text-text-muted">{description}</p> : null}
          </div>
          {children}
        </div>
        {footer ? <div className="mt-6 flex flex-col items-center gap-2 text-center text-sm text-text-muted">{footer}</div> : null}
      </main>
      <p className="m-0 text-xs text-text-muted">Sua marca na tela. Perto do cliente.</p>
    </div>
  )
}
