import { describe, expect, it } from 'vitest'
import {
  endOfDayUtc,
  isTimeZone,
  nextDayStartUtc,
  startOfDayUtc,
  timeZoneLabel,
  todayIn,
  utcToZonedDate,
  utcToZonedLocalInput,
  zonedLocalToUtcIso,
  zonedToUtcIso,
} from './dates'

// Consolidado dos testes de FE-CAMP (lib/dates), FE-OPS (rules: fuso) e FE-ADM (audit/dates).
describe('hora de parede → instante UTC no fuso IANA (nunca o do navegador, nunca offset fixo)', () => {
  it('São Paulo (−03), Manaus (−04), Rio Branco (−05) e Noronha (−02): início/fim do dia', () => {
    expect(endOfDayUtc('2026-10-07')).toBe('2026-10-08T02:59:59Z')
    expect(endOfDayUtc('2026-10-07', 'America/Manaus')).toBe('2026-10-08T03:59:59Z')
    expect(startOfDayUtc('2026-10-07', 'America/Manaus')).toBe('2026-10-07T04:00:00Z')
    expect(startOfDayUtc('2026-10-07', 'America/Rio_Branco')).toBe('2026-10-07T05:00:00Z')
    expect(startOfDayUtc('2026-10-07', 'America/Noronha')).toBe('2026-10-07T02:00:00Z')
  })

  it('horário de verão histórico de São Paulo (−02 em jan/2019; −03 em jul/2019)', () => {
    expect(startOfDayUtc('2019-01-15', 'America/Sao_Paulo')).toBe('2019-01-15T02:00:00Z')
    expect(startOfDayUtc('2019-07-15', 'America/Sao_Paulo')).toBe('2019-07-15T03:00:00Z')
    expect(zonedLocalToUtcIso('2019-01-15T14:30', 'America/Sao_Paulo')).toBe('2019-01-15T16:30:00Z')
  })

  it('datetime-local (comunicado de emergência, agendamentos) nos fusos do Norte', () => {
    expect(zonedLocalToUtcIso('2026-10-07T14:30', 'America/Sao_Paulo')).toBe('2026-10-07T17:30:00Z')
    expect(zonedLocalToUtcIso('2026-10-07T14:30', 'America/Manaus')).toBe('2026-10-07T18:30:00Z')
    expect(zonedLocalToUtcIso('2026-10-07T14:30', 'America/Rio_Branco')).toBe('2026-10-07T19:30:00Z')
    expect(zonedLocalToUtcIso('2026-10-07T22:30', 'America/Rio_Branco')).toBe('2026-10-08T03:30:00Z') // vira o dia em UTC
  })

  it('limite exclusivo "até o dia X" = início do dia seguinte no fuso (filtro de auditoria)', () => {
    expect(nextDayStartUtc('2026-10-07')).toBe('2026-10-08T03:00:00Z')
    expect(nextDayStartUtc('2026-12-31', 'America/Manaus')).toBe('2027-01-01T04:00:00Z')
    expect(nextDayStartUtc('2028-02-28')).toBe('2028-02-29T03:00:00Z') // ano bissexto
  })

  it('fuso inválido/ausente cai no padrão America/Sao_Paulo; entrada inválida → null', () => {
    expect(endOfDayUtc('2026-10-07', 'Fuso/Inexistente')).toBe('2026-10-08T02:59:59Z')
    expect(startOfDayUtc('2026-10-07', null)).toBe('2026-10-07T03:00:00Z')
    expect(zonedToUtcIso('07/10/2026', '00:00')).toBeNull()
    expect(zonedLocalToUtcIso('ontem', 'America/Sao_Paulo')).toBeNull()
    expect(startOfDayUtc('07/10/2026')).toBeNull()
    expect(nextDayStartUtc('')).toBeNull()
    expect(isTimeZone('America/Manaus')).toBe(true)
    expect(isTimeZone('Fuso/Inexistente')).toBe(false)
  })
})

describe('instante UTC → hora de parede no fuso', () => {
  it('data local (virada de dia diferente por fuso)', () => {
    const t = '2026-10-08T03:30:00Z'
    expect(utcToZonedDate(t)).toBe('2026-10-08') // 00:30 em São Paulo
    expect(utcToZonedDate(t, 'America/Manaus')).toBe('2026-10-07') // 23:30 em Manaus
    expect(todayIn('America/Manaus', new Date(t))).toBe('2026-10-07')
  })

  it('valor de datetime-local (ida e volta)', () => {
    expect(utcToZonedLocalInput('2026-10-07T17:30:00Z', 'America/Sao_Paulo')).toBe('2026-10-07T14:30')
    expect(utcToZonedLocalInput('2026-10-08T03:30:00Z', 'America/Rio_Branco')).toBe('2026-10-07T22:30')
    expect(zonedLocalToUtcIso(utcToZonedLocalInput('2026-10-07T18:30:00Z', 'America/Manaus'), 'America/Manaus')).toBe('2026-10-07T18:30:00Z')
  })

  it('rótulo do fuso', () => {
    expect(timeZoneLabel(null)).toBe('horário de Brasília (America/Sao_Paulo)')
    expect(timeZoneLabel('America/Manaus')).toBe('fuso do estabelecimento (America/Manaus)')
  })
})
