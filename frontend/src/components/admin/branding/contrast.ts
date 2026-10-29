/**
 * Contraste WCAG (mesma fórmula do backend instance/service.py) para pré-visualizar as
 * cores da marca: `primary` substitui o token `action` (texto branco por cima, ≥ 4,5:1) e
 * `accent` substitui `brand-blue` (sobre o fundo escuro `canvas` #05070A, ≥ 3:1).
 * O servidor revalida (BRAND_COLOR_CONTRAST).
 */
export const CANVAS_DARK = '#05070A'
export const TEXT_ON_ACTION = '#FFFFFF'
export const DEFAULT_PRIMARY = '#0056B3'
export const DEFAULT_ACCENT = '#0B84FF'
export const HEX_RE = /^#[0-9A-Fa-f]{6}$/

function luminance(hex: string): number {
  const h = hex.replace('#', '')
  const rgb = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255)
  const lin = rgb.map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4))
  return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
}

export function contrastRatio(a: string, b: string): number {
  const la = luminance(a)
  const lb = luminance(b)
  const hi = Math.max(la, lb)
  const lo = Math.min(la, lb)
  return (hi + 0.05) / (lo + 0.05)
}

export function brandColorProblems(primary: string, accent: string): { primary: string | null; accent: string | null } {
  const p = !HEX_RE.test(primary)
    ? 'Use o formato #RRGGBB.'
    : contrastRatio(primary, TEXT_ON_ACTION) < 4.5
      ? `Contraste com texto branco ${contrastRatio(primary, TEXT_ON_ACTION).toFixed(2)}:1 — mínimo 4,5:1.`
      : null
  const a = !HEX_RE.test(accent)
    ? 'Use o formato #RRGGBB.'
    : contrastRatio(accent, CANVAS_DARK) < 3
      ? `Contraste com o fundo escuro ${contrastRatio(accent, CANVAS_DARK).toFixed(2)}:1 — mínimo 3:1.`
      : null
  return { primary: p, accent: a }
}
