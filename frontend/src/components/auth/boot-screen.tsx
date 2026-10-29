import { LogoMark } from '@/components/layout/logo'
import { Spinner } from '@/components/ui/spinner'
import { useBrandName } from '@/components/admin/branding/brand'

/** Tela exibida enquanto a sessão é verificada no boot (refresh + /me). */
export function BootScreen() {
  const brandName = useBrandName()
  return (
    <main className="flex min-h-dvh flex-col items-center justify-center gap-4 p-4" aria-busy="true">
      <h1 className="sr-only">{brandName}</h1>
      <LogoMark className="size-12" title="" />
      <div className="flex items-center gap-2 text-sm text-text-muted">
        <Spinner label="Verificando sua sessão…" />
        <span aria-hidden="true">Verificando sua sessão…</span>
      </div>
    </main>
  )
}
