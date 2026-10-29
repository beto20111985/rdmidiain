import { describe, expect, it } from 'vitest'
import { purgeLegacyAuthStorage } from './legacyStorage'

describe('purgeLegacyAuthStorage', () => {
  it('apaga tokens e estado de login da versão antiga e preserva a preferência de tema', () => {
    localStorage.setItem('rd_access_token', 'a')
    localStorage.setItem('rd_refresh_token', 'b')
    localStorage.setItem('rd-auth', '{"state":{"isAuthenticated":true}}')
    localStorage.setItem('rd-theme', 'light')
    expect(purgeLegacyAuthStorage()).toBe(3)
    expect(Object.keys(localStorage)).toEqual(['rd-theme'])
  })
})
