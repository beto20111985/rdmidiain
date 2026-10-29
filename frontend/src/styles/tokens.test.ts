/**
 * Contraste WCAG dos tokens nos DOIS temas, lido do próprio globals.css (fonte única).
 * Texto ≥ 4,5:1 · bordas de campo, controles e séries de gráfico ≥ 3:1 (1.4.11).
 */
import { describe, expect, it } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'

const css = fs.readFileSync(path.resolve(__dirname, 'globals.css'), 'utf8')

type RGB = [number, number, number]

function block(selectorStart: string): Record<string, string> {
  const i = css.indexOf(selectorStart)
  const open = css.indexOf('{', i)
  const close = css.indexOf('}', open)
  const out: Record<string, string> = {}
  for (const m of css.slice(open + 1, close).matchAll(/--([a-z0-9-]+):\s*([^;]+);/g)) out[m[1]] = m[2].trim()
  return out
}

const dark = block(":root,\n:root[data-theme='dark']")
const light = { ...dark, ...block(":root[data-theme='light']") }

function rgb(theme: Record<string, string>, name: string): RGB {
  const v = theme[name]
  if (!v) throw new Error(`token --${name} ausente`)
  const parts = v.split(/\s+/).map(Number)
  if (parts.length !== 3 || parts.some(Number.isNaN)) throw new Error(`--${name} não é "R G B": ${v}`)
  return parts as RGB
}

function lum([r, g, b]: RGB) {
  const f = (c: number) => {
    const s = c / 255
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
}
export function contrast(a: RGB, b: RGB) {
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}
function mix(fg: RGB, bg: RGB, alpha: number): RGB {
  return fg.map((c, i) => Math.round(c * alpha + bg[i] * (1 - alpha))) as RGB
}

const SURFACES = ['bg', 'sidebar', 'surface', 'surface-raised', 'surface-hover', 'field']
const TEXTS = ['text', 'text-muted', 'link', 'success', 'warning', 'danger', 'info', 'neutral']
const STATUS = ['success', 'warning', 'danger', 'info', 'neutral']

describe.each([
  ['escuro', dark],
  ['claro', light],
])('tema %s', (_name, theme) => {
  it('texto ≥ 4,5:1 em todas as superfícies', () => {
    for (const s of SURFACES) for (const t of TEXTS) {
      const c = contrast(rgb(theme, t), rgb(theme, s))
      expect(c, `${t} sobre ${s} = ${c.toFixed(2)}`).toBeGreaterThanOrEqual(4.5)
    }
  })

  it('badges (cor de status sobre fundo de baixa opacidade) ≥ 4,5:1', () => {
    const alpha = Number(theme['badge-alpha'])
    for (const s of ['surface', 'surface-hover', 'bg']) for (const st of STATUS) {
      const bg = mix(rgb(theme, st), rgb(theme, s), alpha)
      const c = contrast(rgb(theme, st), bg)
      expect(c, `${st} badge sobre ${s} = ${c.toFixed(2)}`).toBeGreaterThanOrEqual(4.5)
    }
  })

  it('texto do botão primário ≥ 4,5:1 (normal e hover)', () => {
    expect(contrast(rgb(theme, 'action-foreground'), rgb(theme, 'action'))).toBeGreaterThanOrEqual(4.5)
    expect(contrast(rgb(theme, 'action-foreground'), rgb(theme, 'action-hover'))).toBeGreaterThanOrEqual(4.5)
  })

  it('borda de campo, foco e séries de gráfico ≥ 3:1 (WCAG 1.4.11)', () => {
    for (const s of SURFACES) {
      expect(contrast(rgb(theme, 'control-border'), rgb(theme, s)), `control-border/${s}`).toBeGreaterThanOrEqual(3)
      expect(contrast(rgb(theme, 'focus'), rgb(theme, s)), `focus/${s}`).toBeGreaterThanOrEqual(3)
    }
    for (const c of ['chart-1', 'chart-2', 'chart-3', 'chart-4']) {
      expect(contrast(rgb(theme, c), rgb(theme, 'surface')), `${c}/surface`).toBeGreaterThanOrEqual(3)
    }
  })

  it('item ativo do menu (gradiente) ≥ 4,5:1 nas duas pontas', () => {
    expect(contrast(rgb(theme, 'nav-active-text'), rgb(theme, 'nav-active-from'))).toBeGreaterThanOrEqual(4.5)
    expect(contrast(rgb(theme, 'nav-active-text'), rgb(theme, 'nav-active-to'))).toBeGreaterThanOrEqual(4.5)
  })
})

describe('tema escuro segue os valores normativos do DS §16', () => {
  it.each([
    ['bg', '5 7 10'],
    ['sidebar', '6 16 32'],
    ['surface', '8 20 38'],
    ['surface-raised', '13 28 50'],
    ['border', '32 56 89'],
    ['control-border', '100 122 150'],
    ['action', '0 86 179'],
    ['action-hover', '6 74 149'],
    ['success', '54 214 172'],
    ['warning', '246 198 106'],
    ['danger', '255 123 134'],
    ['chart-1', '11 132 255'],
    ['chart-2', '0 185 242'],
    ['chart-3', '91 206 255'],
    ['chart-4', '158 175 209'],
  ])('--%s = %s', (name, value) => {
    expect(dark[name]).toBe(value)
  })
})
