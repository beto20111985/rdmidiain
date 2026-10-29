/**
 * Teste de arquitetura (A3 §14.3, P001 §70, E2 #826): o painel não faz polling.
 * Falha se o código de produção usar `refetchInterval` ou `setInterval`.
 */
import { describe, expect, it } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'

const SRC = path.resolve(__dirname, '..')

function walk(dir: string): string[] {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = path.join(dir, e.name)
    if (e.isDirectory()) return walk(p)
    return /\.(ts|tsx)$/.test(e.name) && !/\.test\.(ts|tsx)$/.test(e.name) && !p.includes(`${path.sep}test${path.sep}`) ? [p] : []
  })
}

describe('sem polling no painel', () => {
  const files = walk(SRC)

  it('encontra os arquivos do código-fonte', () => {
    expect(files.length).toBeGreaterThan(30)
  })

  it.each([
    ['refetchInterval', /refetchInterval/],
    ['setInterval', /\bsetInterval\s*\(/],
  ])('nenhum uso de %s', (_n, re) => {
    const offenders = files.filter((f) => re.test(fs.readFileSync(f, 'utf8').replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, '')))
    expect(offenders.map((f) => path.relative(SRC, f))).toEqual([])
  })

  it('o web player não está no bundle do painel', () => {
    expect(fs.existsSync(path.join(SRC, 'pages', 'Player.tsx'))).toBe(false)
    const offenders = files.filter((f) => /['"]\/player\//.test(fs.readFileSync(f, 'utf8')))
    expect(offenders).toEqual([])
  })
})
