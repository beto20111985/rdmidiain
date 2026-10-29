/**
 * Teste de arquitetura (SPEC AÇÃO 003 §11): token de setup/convite, códigos de recuperação, senhas e TOTP
 * NUNCA vão para localStorage/sessionStorage/cookies/IndexedDB — nem pelo código do primeiro acesso,
 * da ativação do 2º Master e da segurança da instância. Também garante que o token do convite só viaja
 * no corpo dos POSTs (nunca em query string de GET).
 */
import { describe, expect, it } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'

const SRC = path.resolve(__dirname, '..', '..')
const FILES = [
  'features/security',
  'features/auth/pages/FirstAccessWizardPage.tsx',
  'features/auth/pages/MasterActivationPage.tsx',
  'features/auth/pages/MfaSetupPage.tsx',
  'features/auth/components/wizard',
  'features/auth/components/TotpEnrollment.tsx',
  'features/auth/components/RecoveryCodesPanel.tsx',
  'features/auth/activationToken.ts',
  'features/auth/setup.ts',
  'components/finance/request-factors-form.tsx',
  'components/finance/authorize-chain.tsx',
  'components/finance/authorize-step.tsx',
  'pages/admin/settings/SecuritySettingsPage.tsx',
]

function collect(rel: string): string[] {
  const p = path.join(SRC, rel)
  if (fs.statSync(p).isFile()) return [p]
  return fs.readdirSync(p, { withFileTypes: true }).flatMap((e) => {
    const full = path.join(p, e.name)
    if (e.isDirectory()) return collect(path.relative(SRC, full))
    return /\.(ts|tsx)$/.test(e.name) && !/\.test\.(ts|tsx)$/.test(e.name) ? [full] : []
  })
}

const strip = (s: string) => s.replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, '')

describe('segredos fora do armazenamento do navegador', () => {
  const files = FILES.flatMap(collect)

  it('encontra os arquivos do escopo', () => {
    expect(files.length).toBeGreaterThan(15)
  })

  it.each([
    ['localStorage', /\blocalStorage\b/],
    ['sessionStorage', /\bsessionStorage\b/],
    ['indexedDB', /\bindexedDB\b/],
    ['document.cookie', /document\.cookie/],
  ])('nenhum uso de %s', (_n, re) => {
    const offenders = files.filter((f) => re.test(strip(fs.readFileSync(f, 'utf8'))))
    expect(offenders.map((f) => path.relative(SRC, f))).toEqual([])
  })

  it('o token do convite nunca entra em query string (só no corpo dos POSTs)', () => {
    const api = strip(fs.readFileSync(path.join(SRC, 'features/auth/api.ts'), 'utf8'))
    expect(api).not.toMatch(/master-activation[^`'"]*\?/)
    expect(api).not.toMatch(/http\.get[^\n]*master-activation/)
    const page = strip(fs.readFileSync(path.join(SRC, 'features/auth/pages/MasterActivationPage.tsx'), 'utf8'))
    expect(page).not.toMatch(/searchParams|useSearchParams|location\.search/)
  })
})
