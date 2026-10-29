import { describe, expect, it } from 'vitest'
import { cityKey, cityKeyLabel, daysFromMask, maskFromDays, validateBands, validateMediaFile, validateRotation, validateSpecificTimes, videoFitsSlot } from '../lib/rules'
import { parseMoneyToCents, parseMultiplierToBps, parsePercentToBps, itemValueText } from '../lib/money'
import { targetText } from '../lib/format'

describe('regras de rotação (W-INV)', () => {
  it('aceita 2+2 e 3+1 min; recusa múltiplos fora de 30 s e ciclo que não divide 1 h', () => {
    expect(validateRotation(120, 120)).toBeNull()
    expect(validateRotation(180, 60)).toBeNull()
    expect(validateRotation(45, 120)).toMatch(/múltiplo de 30/)
    expect(validateRotation(120, 75)).toMatch(/múltiplo de 30/)
    expect(validateRotation(150, 120)).toMatch(/dividir 3600/) // 270 s
  })
})

describe('programação', () => {
  it('dias da semana ↔ máscara (bit 0 = segunda)', () => {
    expect(maskFromDays([0, 1, 2, 3, 4])).toBe(31)
    expect(daysFromMask(96)).toEqual([5, 6])
  })
  it('faixas alinhadas à hora e sem sobreposição', () => {
    expect(validateBands([[0, 1440]])).toBeNull()
    expect(validateBands([[480, 750]])).toMatch(/alinhadas/)
    expect(validateBands([[480, 720], [660, 900]])).toMatch(/sobrepostas/)
    expect(validateBands([])).toMatch(/ao menos/)
  })
  it('horários específicos dentro das faixas e sem repetição', () => {
    expect(validateSpecificTimes(['08:00', '12:30'], [[0, 1440]])).toBeNull()
    expect(validateSpecificTimes(['25:00'], [[0, 1440]])).toMatch(/inválido/)
    expect(validateSpecificTimes(['07:00'], [[480, 720]])).toMatch(/fora das faixas/)
    expect(validateSpecificTimes(['08:00', '08:00'], [[0, 1440]])).toMatch(/repetidos/)
  })
  it('vídeo cabe no slot com a tolerância do servidor (−1000 ms / +40 ms); nunca corta', () => {
    expect(videoFitsSlot(30000, 30)).toBe(true)
    expect(videoFitsSlot(29001, 30)).toBe(true)
    expect(videoFitsSlot(31200, 30)).toBe(false)
    expect(videoFitsSlot(null, 30)).toBeNull()
  })
})

describe('mídia', () => {
  it('limites por tipo: imagem 10 MB, vídeo 200 MB; tipos não aceitos', () => {
    const lim = { image: 10 * 1024 * 1024, video: 200 * 1024 * 1024 }
    expect(validateMediaFile(new File([new Uint8Array(5)], 'a.png', { type: 'image/png' }), lim)).toBeNull()
    expect(validateMediaFile(new File([new Uint8Array(5)], 'a.webp', { type: 'image/webp' }), lim)).toMatch(/Formato não aceito/)
    expect(validateMediaFile(new File([], 'a.mp4', { type: 'video/mp4' }), lim)).toMatch(/vazio/)
  })
})

describe('cidade e segmentação', () => {
  it('chave de cidade igual à do backend', () => {
    expect(cityKey('go', 'Goiânia')).toBe('GO:goiania')
    expect(cityKey('SP', 'São José dos Campos')).toBe('SP:sao-jose-dos-campos')
    expect(cityKeyLabel('GO:goiania')).toBe('Goiania (GO)')
  })
  it('texto de alvo de estabelecimento não expõe identificador sem nome', () => {
    expect(targetText({ type: 'ESTABLISHMENT', value: '0b8c0000-0000-4000-8000-000000000000', exclude: false })).toBe(
      'Incluir · Estabelecimento específico: Estabelecimento selecionado pela RD',
    )
    expect(targetText({ type: 'REGION', value: 'CENTRO_OESTE', exclude: true })).toBe('Excluir · Região: Centro-Oeste')
  })
})

describe('entrada de valores (cadastro de preços)', () => {
  it('converte texto pt-BR para centavos e bps', () => {
    expect(parseMoneyToCents('1.234,56')).toBe(123456)
    expect(parseMoneyToCents('10')).toBe(1000)
    expect(parseMoneyToCents('abc')).toBeNull()
    expect(parsePercentToBps('12,5')).toBe(1250)
    expect(parseMultiplierToBps('1,25')).toBe(12500)
    expect(itemValueText('MULTIPLIER_BPS', 12500)).toBe('× 1,25')
    expect(itemValueText('PERCENT_BPS', 500)).toBe('5%')
  })
})
