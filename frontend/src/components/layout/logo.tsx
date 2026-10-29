/**
 * Logo RD Mídia Indoor — PLACEHOLDER fiel à composição do DS §4 enquanto a matriz vetorial
 * oficial não existe (DS §19): monograma RD à esquerda, "MÍDIA" em destaque e "INDOOR" abaixo
 * com espaçamento, moldura azul arredondada remetendo à tela. Compacta: monograma branco em
 * campo azul (menu recolhido). NÃO é a prancha PNG; substituir pelos SVGs oficiais.
 */
import * as React from 'react'
import { cn } from '@/lib/utils'
import { useBrandStore } from '@/components/admin/branding/brand'

/**
 * White-label (REQ-NEG-005): com marca da instância configurada, mostra a logo/nome dela;
 * sem marca (padrão) — ou se a imagem falhar — o logo RD abaixo, idêntico ao original.
 */
function useInstanceLogo() {
  const brand = useBrandStore((s) => s.brand)
  const [broken, setBroken] = React.useState<string | null>(null)
  const logoUrl = brand.logoUrl && brand.logoUrl !== broken ? brand.logoUrl : null
  return { brand, logoUrl, onError: () => setBroken(brand.logoUrl) }
}

function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]!.toUpperCase())
    .join('')
}

export function LogoMark({ className, title = 'RD Mídia Indoor' }: { className?: string; title?: string }) {
  const { brand, logoUrl, onError } = useInstanceLogo()
  if (!brand.isDefault) {
    if (logoUrl)
      return <img src={logoUrl} alt={title ? brand.name : ''} onError={onError} className={cn('size-10 shrink-0 rounded-[10px] object-contain', className)} />
    return (
      <span
        role={title ? 'img' : undefined}
        aria-label={title ? brand.name : undefined}
        aria-hidden={title ? undefined : true}
        className={cn('inline-flex size-10 shrink-0 items-center justify-center rounded-[10px] bg-brand-blue text-[15px] font-extrabold text-white', className)}
      >
        {initials(brand.name)}
      </span>
    )
  }
  return (
    <svg viewBox="0 0 40 40" role="img" aria-label={title} className={cn('size-10 shrink-0', className)}>
      <rect width="40" height="40" rx="10" fill="var(--brand-blue)" />
      <rect x="0.75" y="0.75" width="38.5" height="38.5" rx="9.25" fill="none" stroke="rgb(255 255 255 / 0.18)" strokeWidth="1.5" />
      <text
        x="20"
        y="26.5"
        textAnchor="middle"
        fontFamily="Manrope, Inter, system-ui, sans-serif"
        fontWeight="800"
        fontSize="17"
        letterSpacing="-0.5"
        fill="#ffffff"
      >
        RD
      </text>
    </svg>
  )
}

/** Horizontal: largura mínima 180 px (DS §4). */
export function LogoHorizontal({ className }: { className?: string }) {
  const { brand, logoUrl, onError } = useInstanceLogo()
  if (!brand.isDefault) {
    return (
      <span className={cn('inline-flex min-w-[180px] max-w-[220px] items-center gap-3 rounded-[10px] border-[1.5px] border-brand-blue px-2 py-1.5', className)}>
        {logoUrl ? (
          <img src={logoUrl} alt="" onError={onError} className="h-9 max-w-[120px] shrink-0 object-contain" />
        ) : (
          <LogoMark className="size-9" title="" />
        )}
        <span className="min-w-0 truncate text-[15px] font-extrabold text-text" aria-hidden="true">
          {brand.name}
        </span>
        <span className="sr-only">{brand.name}</span>
      </span>
    )
  }
  return (
    <span className={cn('inline-flex min-w-[180px] items-center gap-3 rounded-[10px] border-[1.5px] border-brand-blue px-2 py-1.5', className)}>
      <LogoMark className="size-9" title="" />
      <span className="flex flex-col leading-none" aria-hidden="true">
        <span className="text-[18px] font-extrabold tracking-[0.02em] text-text">MÍDIA</span>
        {/* Logotipo: isento do critério 1.4.3; cor da marca (azul) conforme DS §4 */}
        <span className="mt-1 text-[12px] font-bold tracking-[0.3em] text-brand-blue">INDOOR</span>
      </span>
      <span className="sr-only">RD Mídia Indoor</span>
    </span>
  )
}
