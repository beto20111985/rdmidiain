import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { downloadFile, filenameFromDisposition } from './download'
import { parseBRLToCents, parsePercentToBps, centsToInput, bpsToInput } from './money'
import { daysBetween, fillDailySeries, presetRange, shiftDate, validatePeriod } from './period'
import { todayIn } from '@/lib/dates'
import { formatBps, formatCents } from '@/lib/format'
import { tokenStore } from '@/lib/http'
import { ApiError } from '@/lib/errors'
import { isCpfOrCnpjShape, paymentErrorInfo, pixKeyFieldError, stripDocumentMask } from './payment-errors'
import { personLabel } from './names'
import { disambiguatedNames } from './hooks'

const nbsp = (s: string) => s.replace(new RegExp(String.fromCharCode(160), 'g'), ' ')

describe('dinheiro: centavos ↔ BRL', () => {
  it('formata centavos em BRL pt-BR (sem float na API)', () => {
    expect(nbsp(formatCents(123456))).toBe('R$ 1.234,56')
    expect(nbsp(formatCents(5))).toBe('R$ 0,05')
    expect(nbsp(formatCents(-1999))).toMatch(/-\s?R\$ 19,99/)
    expect(formatCents(null)).toBe('—')
    expect(nbsp(formatBps(500))).toBe('5,00%')
  })
  it('converte o valor digitado em centavos inteiros', () => {
    expect(parseBRLToCents('1.234,56')).toBe(123456)
    expect(parseBRLToCents('R$ 10')).toBe(1000)
    expect(parseBRLToCents('0,5')).toBe(50)
    expect(parseBRLToCents('1234,5')).toBe(123450)
    expect(parseBRLToCents('12.34')).toBeNull() // ponto decimal ambíguo → inválido
    expect(parseBRLToCents('abc')).toBeNull()
    expect(parseBRLToCents('')).toBeNull()
    expect(centsToInput(123456)).toBe('1234,56')
  })
  it('percentual digitado ↔ basis points', () => {
    expect(parsePercentToBps('12,5')).toBe(1250)
    expect(parsePercentToBps('5')).toBe(500)
    expect(parsePercentToBps('101')).toBeNull()
    expect(bpsToInput(1250)).toBe('12,50')
    expect(bpsToInput(500)).toBe('5')
  })
})

describe('período no fuso America/Sao_Paulo', () => {
  it('hoje em SP independe do fuso do navegador', () => {
    // 2026-10-08 01:30 UTC = 2026-10-07 22:30 em São Paulo
    expect(todayIn('America/Sao_Paulo', new Date('2026-10-08T01:30:00Z'))).toBe('2026-10-07')
    expect(presetRange('7', 'America/Sao_Paulo', new Date('2026-10-08T01:30:00Z'))).toEqual({ start: '2026-10-01', end: '2026-10-07' })
    expect(presetRange('hoje', 'America/Sao_Paulo', new Date('2026-10-08T01:30:00Z'))).toEqual({ start: '2026-10-07', end: '2026-10-07' })
  })
  it('valida período personalizado (ordem e limite de 366 dias)', () => {
    expect(validatePeriod('2026-10-07', '2026-10-01')).toMatch(/anterior/)
    expect(validatePeriod('2025-01-01', '2026-10-01')).toMatch(/366/)
    expect(validatePeriod('2026-10-01', '2026-10-07')).toBeNull()
    expect(daysBetween('2026-10-01', '2026-10-07')).toBe(7)
    expect(shiftDate('2026-03-01', -1)).toBe('2026-02-28')
  })
})

describe('download autenticado (fetch com Bearer → arquivo)', () => {
  const created: string[] = []
  let clicked: string | null = null
  beforeEach(() => {
    tokenStore.set('token-de-teste')
    created.length = 0
    clicked = null
    vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: vi.fn(() => 'blob:teste'), revokeObjectURL: vi.fn() }))
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      clicked = this.download
    })
  })
  afterEach(() => {
    tokenStore.clear()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('envia Authorization Bearer (sem token na URL) e salva com o nome do Content-Disposition', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
      created.push(String(input))
      expect((init.headers as Record<string, string>).Authorization).toBe('Bearer token-de-teste')
      return new Response(new Blob(['%PDF-1.7 teste'], { type: 'application/pdf' }), {
        status: 200,
        headers: { 'content-type': 'application/pdf', 'content-disposition': 'attachment; filename="comprovante-CV-ABCD2345.pdf"', 'x-content-sha256': 'f'.repeat(64) },
      })
    })
    vi.stubGlobal('fetch', fetchMock)
    const r = await downloadFile('/reports/proofs/p1/pdf', { fallbackFilename: 'x.pdf' })
    expect(created[0]).toBe('/api/v1/reports/proofs/p1/pdf')
    expect(created[0]).not.toMatch(/token/)
    expect(r.filename).toBe('comprovante-CV-ABCD2345.pdf')
    expect(r.sha256).toBe('f'.repeat(64))
    expect(clicked).toBe('comprovante-CV-ABCD2345.pdf')
    expect(URL.createObjectURL).toHaveBeenCalled()
  })

  it('erro do servidor vira ApiError com a mensagem do envelope (nada é salvo)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify({ error: { code: 'EXPORT_EXPIRED', message: 'Arquivo expirado; gere novamente.' } }), { status: 410, headers: { 'content-type': 'application/json' } })),
    )
    await expect(downloadFile('/reports/exports/e1/download', { fallbackFilename: 'r.csv' })).rejects.toMatchObject({ code: 'EXPORT_EXPIRED', message: 'Arquivo expirado; gere novamente.' })
    await expect(downloadFile('/reports/exports/e1/download', { fallbackFilename: 'r.csv' })).rejects.toBeInstanceOf(ApiError)
    expect(clicked).toBeNull()
  })

  it('extrai nomes RFC 5987 e protege contra caminhos', () => {
    expect(filenameFromDisposition("attachment; filename*=UTF-8''relat%C3%B3rio.xlsx", 'f')).toBe('relatório.xlsx')
    expect(filenameFromDisposition('attachment; filename="../../etc/passwd"', 'f')).toBe('.._.._etc_passwd')
    expect(filenameFromDisposition(null, 'padrao.pdf')).toBe('padrao.pdf')
  })
})

describe('documentos e erros de pagamento (CNPJ alfanumérico)', () => {
  const err = (code: string, message: string, details: Record<string, unknown> = {}) => new ApiError({ status: 422, code, message, details })
  it('aceita CPF, CNPJ numérico e CNPJ alfanumérico removendo só a máscara', () => {
    expect(isCpfOrCnpjShape('123.456.789-09')).toBe(true)
    expect(isCpfOrCnpjShape('11.222.333/0001-81')).toBe(true)
    expect(isCpfOrCnpjShape('12.abc.345/01de-35')).toBe(true)
    expect(stripDocumentMask('12.abc.345/01de-35')).toBe('12ABC34501DE35')
    expect(isCpfOrCnpjShape('11222333000181X')).toBe(false)
    expect(isCpfOrCnpjShape('12ABC34501DEXY')).toBe(false) // DV precisa ser numérico
  })
  it('CNPJ alfanumérico não suportado pelo gateway = dependência externa (não pede correção do cadastro)', () => {
    const i = paymentErrorInfo(err('PAYER_DOCUMENT_NOT_SUPPORTED_BY_GATEWAY', 'O meio de pagamento (CloudPayPag) ainda não aceita CNPJ alfanumérico.'))
    expect(i?.kind).toBe('external')
    expect(i?.title).toBe('BLOQUEADO POR DEPENDÊNCIA EXTERNA')
    const inc = paymentErrorInfo(err('PAYER_DATA_INCOMPLETE', 'Cadastro incompleto.', { missing: ['document', 'email'] }))
    expect(inc?.kind).toBe('fix-registration')
    expect(inc?.message).toMatch(/Faltando: CPF\/CNPJ, e-mail/)
    expect(pixKeyFieldError(err('PIX_KEY_CNPJ_ALPHANUMERIC_NOT_SUPPORTED', 'Chave CNPJ alfanumérica não suportada.'))).toMatch(/Use outro tipo de chave/)
    expect(paymentErrorInfo(err('OUTRO', 'x'))).toBeNull()
  })
  it('nomes vêm do servidor; sem nome → id curto (nunca inventado)', () => {
    expect(personLabel('Ana Souza', 'u1', 'u2')).toBe('Ana Souza')
    expect(personLabel('Ana Souza', 'u1', 'u1')).toBe('Ana Souza (você)')
    expect(personLabel(null, 'abcdef0123456', null)).toBe('Usuário abcdef01')
    expect(personLabel(null, null)).toBe('—')
  })
})

describe('séries e rótulos', () => {
  it('preenche dias sem exibição com zero dentro do período', () => {
    expect(fillDailySeries([{ day: '2026-10-02', valid: 5 }], '2026-10-01', '2026-10-03')).toEqual([
      { day: '2026-10-01', valid: 0 },
      { day: '2026-10-02', valid: 5 },
      { day: '2026-10-03', valid: 0 },
    ])
  })

  it('diferencia organizações com o mesmo nome fantasia', () => {
    const m = disambiguatedNames([
      { id: 'aaaaaa-1', name: 'Alfa Ltda', trade_name: 'Alfa' },
      { id: 'bbbbbb-2', name: 'Alfa Comércio', trade_name: 'Alfa' },
      { id: 'cccccc-3', name: 'Beta', trade_name: null },
      { id: 'dddddd-4', name: 'Gama', trade_name: 'Gama' },
      { id: 'eeeeee-5', name: 'Gama', trade_name: 'Gama' },
    ])
    expect(m.get('aaaaaa-1')).toBe('Alfa (Alfa Ltda)')
    expect(m.get('bbbbbb-2')).toBe('Alfa (Alfa Comércio)')
    expect(m.get('cccccc-3')).toBe('Beta')
    expect(m.get('dddddd-4')).toBe('Gama · dddddd')
    expect(m.get('eeeeee-5')).toBe('Gama · eeeeee')
  })
})
