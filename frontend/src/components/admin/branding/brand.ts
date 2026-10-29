/**
 * Marca da instância (white-label — REQ-NEG-005), lida de `GET /instance/branding` (público).
 *
 * - Nome, logo e cores entram SOMENTE pelos tokens do Design System: `--action` /
 *   `--action-hover` (botão primário, texto claro por cima) e `--brand-blue` (destaque).
 *   Nenhuma cor livre é aplicada fora desses tokens.
 * - Contraste revalidado no cliente: cor principal ≥ 4,5:1 com o texto do botão nos DOIS
 *   temas (#FFFFFF e #F5F7FA); destaque ≥ 3:1 com o fundo escuro (#05070A) e com o branco
 *   (o token é o mesmo nos dois temas). Cor reprovada → token padrão RD (nada é forçado).
 * - Sem marca configurada (ou erro de rede) → nenhuma variável é sobrescrita: aparência RD
 *   padrão idêntica (o CSS de globals.css continua valendo).
 * - Nada é salvo no navegador (estado só em memória).
 */
import { create } from 'zustand'
import { CANVAS_DARK, HEX_RE, contrastRatio } from './contrast'

export const RD_BRAND_NAME = 'RD Mídia Indoor'
export const RD_PRIMARY = '#0056B3'
export const RD_ACCENT = '#0B84FF'
export const DEFAULT_FAVICON = { href: '/favicon.svg', type: 'image/svg+xml' }

/** Resposta de GET /instance/branding (W-BIZ `profile_public` + `logo_url`). */
export interface BrandingResponse {
  instance_slug?: string | null
  display_name?: string | null
  is_white_label?: boolean
  primary_color?: string | null
  accent_color?: string | null
  has_logo?: boolean
  logo_url?: string | null
  support_email?: string | null
  support_phone?: string | null
}

export interface Brand {
  name: string
  /** true = marca RD padrão (logo SVG do DS, sem variáveis sobrescritas). */
  isDefault: boolean
  logoUrl: string | null
  /** Cor principal aplicada (null = token padrão). */
  primary: string | null
  /** Cor de destaque aplicada (null = token padrão). */
  accent: string | null
  /** Cores recebidas e recusadas por contraste (para diagnóstico/teste). */
  rejected: Array<'primary' | 'accent'>
}

export const DEFAULT_BRAND: Brand = { name: RD_BRAND_NAME, isDefault: true, logoUrl: null, primary: null, accent: null, rejected: [] }

const BUTTON_TEXT = ['#FFFFFF', '#F5F7FA'] // --action-foreground dos temas claro e escuro

export function primaryAcceptable(hex: string): boolean {
  return HEX_RE.test(hex) && BUTTON_TEXT.every((t) => contrastRatio(hex, t) >= 4.5)
}

export function accentAcceptable(hex: string): boolean {
  return HEX_RE.test(hex) && contrastRatio(hex, CANVAS_DARK) >= 3 && contrastRatio(hex, '#FFFFFF') >= 3
}

const same = (a: string | null | undefined, b: string) => (a ?? '').toUpperCase() === b.toUpperCase()

/** Converte a resposta do servidor na marca efetiva (com as regras de contraste). */
export function resolveBrand(raw: BrandingResponse | null | undefined, version?: number): Brand {
  if (!raw) return DEFAULT_BRAND
  const name = (raw.display_name ?? '').trim() || RD_BRAND_NAME
  const rejected: Brand['rejected'] = []
  let primary: string | null = null
  if (raw.primary_color && !same(raw.primary_color, RD_PRIMARY)) {
    if (primaryAcceptable(raw.primary_color)) primary = raw.primary_color.toUpperCase()
    else rejected.push('primary')
  }
  let accent: string | null = null
  if (raw.accent_color && !same(raw.accent_color, RD_ACCENT)) {
    if (accentAcceptable(raw.accent_color)) accent = raw.accent_color.toUpperCase()
    else rejected.push('accent')
  }
  const base = raw.has_logo ? raw.logo_url || '/api/v1/instance/logo' : null
  const logoUrl = base ? `${base}${base.includes('?') ? '&' : '?'}v=${version ?? 0}` : null
  const isDefault = name === RD_BRAND_NAME && !logoUrl && !primary && !accent
  return { name, isDefault, logoUrl, primary, accent, rejected }
}

function hexToChannels(hex: string): string {
  const h = hex.replace('#', '')
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)).join(' ')
}

/** Hover do botão: 15% mais escuro (contraste com texto claro só aumenta). */
function hoverChannels(hex: string): string {
  const h = hex.replace('#', '')
  return [0, 2, 4].map((i) => Math.round(parseInt(h.slice(i, i + 2), 16) * 0.85)).join(' ')
}

const VARS = ['--action', '--action-hover', '--brand-blue'] as const

/** Aplica (ou remove) os tokens da marca, o favicon e nada mais. Seguro fora do navegador. */
export function applyBrandToDocument(brand: Brand, doc: Document | undefined = typeof document !== 'undefined' ? document : undefined) {
  if (!doc) return
  const style = doc.documentElement.style
  VARS.forEach((v) => style.removeProperty(v))
  if (brand.primary) {
    style.setProperty('--action', hexToChannels(brand.primary))
    style.setProperty('--action-hover', hoverChannels(brand.primary))
  }
  if (brand.accent) style.setProperty('--brand-blue', brand.accent.toLowerCase())

  let icon = doc.querySelector<HTMLLinkElement>('link[rel="icon"]')
  if (!icon) {
    icon = doc.createElement('link')
    icon.rel = 'icon'
    doc.head.appendChild(icon)
  }
  if (brand.logoUrl) {
    icon.setAttribute('href', brand.logoUrl)
    icon.removeAttribute('type')
  } else {
    icon.setAttribute('href', DEFAULT_FAVICON.href)
    icon.setAttribute('type', DEFAULT_FAVICON.type)
  }
}

interface BrandState {
  brand: Brand
  setBrand: (b: Brand) => void
}

/** Marca efetiva em memória (lida pelo logo, títulos e links); padrão RD até o servidor responder. */
export const useBrandStore = create<BrandState>((set) => ({
  brand: DEFAULT_BRAND,
  setBrand: (brand) => set({ brand }),
}))

export function useBrandName(): string {
  return useBrandStore((s) => s.brand.name)
}

/** "Página · Marca" (ou só a marca). */
export function pageTitle(title: string | null | undefined, brandName: string): string {
  return !title || title === brandName || title === RD_BRAND_NAME ? brandName : `${title} · ${brandName}`
}
