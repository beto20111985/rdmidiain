/**
 * Spec do editor de templates — espelho do contrato do backend (app/modules/templates/spec.py):
 * grade 24 × 24 sobre 1920×1080 (H) ou 1080×1920 (V); camadas `text` | `image` | `rect`;
 * fontes do servidor; variáveis `{{nome}}` declaradas; renderização SOMENTE no servidor.
 * O editor restringe as cores aos tokens do design system RD (§5).
 * `validateSpec` antecipa os erros que o servidor devolveria (TEMPLATE_SPEC_INVALID).
 */
export const GRID = 24
export const CANVAS = { HORIZONTAL: { width: 1920, height: 1080 }, VERTICAL: { width: 1080, height: 1920 } } as const
export type Orientation = keyof typeof CANVAS

export const DEFAULT_FONTS = ['dejavu-mono', 'dejavu-sans', 'dejavu-sans-bold', 'dejavu-serif', 'dejavu-serif-bold'] as const
export const FONT_LABELS: Record<string, string> = {
  'dejavu-sans': 'Sans',
  'dejavu-sans-bold': 'Sans negrito',
  'dejavu-serif': 'Serifada',
  'dejavu-serif-bold': 'Serifada negrito',
  'dejavu-mono': 'Monoespaçada',
}

/** Cores permitidas no editor = tokens do DS RD (§5). */
export const TOKEN_COLORS: Array<{ token: string; hex: string; label: string }> = [
  { token: 'brand-black', hex: '#05070A', label: 'Preto RD' },
  { token: 'brand-graphite', hex: '#0D1117', label: 'Grafite' },
  { token: 'brand-deep-blue', hex: '#082A4A', label: 'Azul profundo' },
  { token: 'action', hex: '#0056B3', label: 'Azul ação' },
  { token: 'brand-blue', hex: '#0B84FF', label: 'Azul RD' },
  { token: 'cyan', hex: '#00B9F2', label: 'Ciano' },
  { token: 'brand-light-blue', hex: '#5BCEFF', label: 'Azul claro' },
  { token: 'brand-white', hex: '#F5F7FA', label: 'Branco RD' },
  { token: 'brand-gray', hex: '#A6AFBB', label: 'Cinza' },
  { token: 'success', hex: '#36D6AC', label: 'Verde' },
  { token: 'warning', hex: '#F6C66A', label: 'Amarelo' },
  { token: 'danger', hex: '#FF7B86', label: 'Vermelho' },
]
const TOKEN_HEX = new Set(TOKEN_COLORS.map((c) => c.hex.toUpperCase()))
export function isTokenColor(hex: string): boolean {
  return TOKEN_HEX.has(hex.toUpperCase())
}

export interface Box {
  col: number
  row: number
  col_span: number
  row_span: number
}

export interface TextLayer {
  type: 'text'
  box: Box
  text: string
  font: string
  size_px: number
  color: string
  align: 'left' | 'center' | 'right'
  valign: 'top' | 'middle' | 'bottom'
  max_lines: number
  uppercase: boolean
}

export interface ImageLayer {
  type: 'image'
  box: Box
  asset_id: string
  fit: 'contain' | 'cover'
}

export interface RectLayer {
  type: 'rect'
  box: Box
  color: string
  opacity: number
  radius_px: number
}

export type Layer = TextLayer | ImageLayer | RectLayer

export interface Variable {
  name: string
  label: string
  max_len: number
  default?: string | null
}

export interface TemplateSpec {
  background: { color: string }
  variables: Variable[]
  layers: Layer[]
}

/** Estado do editor: camadas com `uid` só para a interface (removido no `toSpec`). */
export type EditorLayer = Layer & { uid: string }
export interface EditorState {
  background: string
  variables: Variable[]
  layers: EditorLayer[]
}

let uidSeq = 0
export function nextUid(): string {
  uidSeq += 1
  return `l${Date.now().toString(36)}${uidSeq}`
}

export function newLayer(type: Layer['type']): EditorLayer {
  const uid = nextUid()
  if (type === 'text')
    return {
      uid,
      type: 'text',
      box: { col: 2, row: 2, col_span: 20, row_span: 4 },
      text: 'Seu texto aqui',
      font: 'dejavu-sans-bold',
      size_px: 96,
      color: '#F5F7FA',
      align: 'center',
      valign: 'middle',
      max_lines: 2,
      uppercase: false,
    }
  if (type === 'image') return { uid, type: 'image', box: { col: 8, row: 8, col_span: 8, row_span: 8 }, asset_id: '', fit: 'contain' }
  return { uid, type: 'rect', box: { col: 0, row: 18, col_span: 24, row_span: 6 }, color: '#0B84FF', opacity: 90, radius_px: 0 }
}

export function emptyEditor(): EditorState {
  return { background: '#05070A', variables: [], layers: [newLayer('rect'), newLayer('text')] }
}

/** Converte a spec recebida do servidor em estado de edição. */
export function fromSpec(raw: unknown): EditorState {
  const s = (raw ?? {}) as Partial<TemplateSpec>
  return {
    background: s.background?.color ?? '#05070A',
    variables: (s.variables ?? []).map((v) => ({ name: v.name, label: v.label, max_len: v.max_len ?? 80, default: v.default ?? null })),
    layers: (s.layers ?? []).map((l) => ({ ...l, uid: nextUid() }) as EditorLayer),
  }
}

/** Spec exatamente no formato do backend (inteiros, sem campos de interface). */
export function toSpec(state: EditorState): TemplateSpec {
  const box = (b: Box): Box => ({ col: Math.trunc(b.col), row: Math.trunc(b.row), col_span: Math.trunc(b.col_span), row_span: Math.trunc(b.row_span) })
  return {
    background: { color: state.background.toUpperCase() },
    variables: state.variables.map((v) => {
      const out: Variable = { name: v.name.trim(), label: v.label.trim(), max_len: Math.trunc(v.max_len) }
      if (v.default) out.default = v.default
      return out
    }),
    layers: state.layers.map((l): Layer => {
      if (l.type === 'text')
        return {
          type: 'text',
          box: box(l.box),
          text: l.text,
          font: l.font,
          size_px: Math.trunc(l.size_px),
          color: l.color.toUpperCase(),
          align: l.align,
          valign: l.valign,
          max_lines: Math.trunc(l.max_lines),
          uppercase: !!l.uppercase,
        }
      if (l.type === 'image') return { type: 'image', box: box(l.box), asset_id: l.asset_id, fit: l.fit }
      return { type: 'rect', box: box(l.box), color: l.color.toUpperCase(), opacity: Math.trunc(l.opacity), radius_px: Math.trunc(l.radius_px) }
    }),
  }
}

const HEX = /^#[0-9A-Fa-f]{6}$/
const VAR_NAME = /^[a-z][a-z0-9_]{0,29}$/
const VAR_RE = /\{\{\s*([a-z][a-z0-9_]{0,29})\s*\}\}/g
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const int = (n: unknown, lo: number, hi: number) => typeof n === 'number' && Number.isInteger(n) && n >= lo && n <= hi

/** Problemas (pt-BR) que o servidor recusaria. Lista vazia = spec válida. */
export function validateSpec(spec: TemplateSpec, fonts: readonly string[] = DEFAULT_FONTS): string[] {
  const p: string[] = []
  if (!HEX.test(spec.background.color)) p.push('Fundo: cor inválida.')
  if (spec.variables.length > 20) p.push('Máximo de 20 variáveis.')
  if (spec.layers.length > 30) p.push('Máximo de 30 camadas.')
  const names = spec.variables.map((v) => v.name)
  if (new Set(names).size !== names.length) p.push('Variáveis com nomes repetidos.')
  spec.variables.forEach((v, i) => {
    if (!VAR_NAME.test(v.name)) p.push(`Variável ${i + 1}: nome deve começar com letra minúscula e usar só a–z, 0–9 e “_”.`)
    if (!v.label || v.label.length > 60) p.push(`Variável ${i + 1}: rótulo de 1 a 60 caracteres.`)
    if (!int(v.max_len, 1, 300)) p.push(`Variável ${i + 1}: tamanho máximo de 1 a 300.`)
    if (v.default && v.default.length > 300) p.push(`Variável ${i + 1}: valor padrão muito longo.`)
  })
  const declared = new Set(names)
  spec.layers.forEach((l, i) => {
    const n = `Camada ${i + 1}`
    const b = l.box
    if (!int(b.col, 0, GRID - 1) || !int(b.row, 0, GRID - 1) || !int(b.col_span, 1, GRID) || !int(b.row_span, 1, GRID))
      p.push(`${n}: posição/tamanho fora da grade 24×24.`)
    else if (b.col + b.col_span > GRID || b.row + b.row_span > GRID) p.push(`${n}: a caixa ultrapassa a grade 24×24.`)
    if (l.type === 'text') {
      if (!l.text || l.text.length > 500) p.push(`${n}: texto de 1 a 500 caracteres.`)
      if (!fonts.includes(l.font)) p.push(`${n}: fonte não permitida.`)
      if (!int(l.size_px, 12, 400)) p.push(`${n}: tamanho da fonte de 12 a 400 px.`)
      if (!HEX.test(l.color)) p.push(`${n}: cor inválida.`)
      if (!int(l.max_lines, 1, 12)) p.push(`${n}: linhas de 1 a 12.`)
      const used = Array.from(l.text.matchAll(VAR_RE), (m) => m[1])
      const missing = used.filter((u) => !declared.has(u))
      if (missing.length) p.push(`${n}: variáveis não declaradas: ${Array.from(new Set(missing)).join(', ')}.`)
      if (/\{\{|\}\}/.test(l.text.replace(VAR_RE, ''))) p.push(`${n}: use variáveis só no formato {{nome}}.`)
    } else if (l.type === 'image') {
      if (!UUID.test(l.asset_id)) p.push(`${n}: envie a imagem da camada.`)
    } else {
      if (!HEX.test(l.color)) p.push(`${n}: cor inválida.`)
      if (!int(l.opacity, 0, 100)) p.push(`${n}: opacidade de 0 a 100.`)
      if (!int(l.radius_px, 0, 200)) p.push(`${n}: arredondamento de 0 a 200 px.`)
    }
  })
  return p
}

/** Substitui {{var}} pelos valores (ou rótulo) — usado só na pré-visualização aproximada. */
export function previewText(text: string, variables: Variable[], values: Record<string, string>): string {
  return text.replace(VAR_RE, (_m, name: string) => {
    const v = variables.find((x) => x.name === name)
    return values[name] || v?.default || (v ? `‹${v.label}›` : '')
  })
}

/** Limita a caixa à grade (ao editar números). */
export function clampBox(b: Box): Box {
  const col = Math.min(Math.max(0, Math.trunc(b.col) || 0), GRID - 1)
  const row = Math.min(Math.max(0, Math.trunc(b.row) || 0), GRID - 1)
  const col_span = Math.min(Math.max(1, Math.trunc(b.col_span) || 1), GRID - col)
  const row_span = Math.min(Math.max(1, Math.trunc(b.row_span) || 1), GRID - row)
  return { col, row, col_span, row_span }
}
