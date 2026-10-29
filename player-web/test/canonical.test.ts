import { describe, expect, it } from 'vitest';
import { CanonicalError, canonicalBytes, canonicalize, compareCodePoints, JsonFloat } from '../src/core/canonical';

describe('RDJ1 — JSON canônico', () => {
  it('ordena chaves por ponto de código, sem espaços', () => {
    expect(canonicalize({ b: 1, a: 2, A: 3, aa: 4, '': 5 })).toBe('{"":5,"A":3,"a":2,"aa":4,"b":1}');
  });

  it('ordena chaves aninhadas e preserva a ordem de arrays', () => {
    const v = { z: { y: [3, 1, { d: true, c: null }], x: 'v' }, a: [] as unknown[] };
    expect(canonicalize(v)).toBe('{"a":[],"z":{"x":"v","y":[3,1,{"c":null,"d":true}]}}');
  });

  it('ordena por ponto de código (não por unidade UTF-16) com caracteres fora do BMP', () => {
    // U+1F600 (😀, surrogates D83D DE00) vs U+FF61 (｡): por UTF-16 D83D < FF61; por ponto de código 1F600 > FF61.
    const keys = ['\u{1F600}', '｡', 'a'];
    expect([...keys].sort(compareCodePoints)).toEqual(['a', '｡', '\u{1F600}']);
    expect(canonicalize({ '\u{1F600}': 1, '｡': 2 })).toBe('{"｡":2,"\u{1F600}":1}');
  });

  it('não escapa não-ASCII (UTF-8 cru) e usa escapes mínimos', () => {
    expect(canonicalize('ação — Mídia ☕ 😀')).toBe('"ação — Mídia ☕ 😀"');
    expect(canonicalize('a"b\\c/d')).toBe('"a\\"b\\\\c/d"');
    expect(canonicalize('\b\f\n\r\t')).toBe('"\\b\\f\\n\\r\\t"');
    expect(canonicalize('\u0000\u0001\u001f\u007f')).toBe('"\\u0000\\u0001\\u001f\u007f"');
    expect(canonicalize('  ')).toBe('"  "');
  });

  it('controles viram \\u00xx com hex minúsculo', () => {
    expect(canonicalize('\u001b\u000b')).toBe('"\\u001b\\u000b"');
  });

  it('bytes são o UTF-8 do texto canônico', () => {
    const bytes = canonicalBytes({ k: 'é' });
    expect(Array.from(bytes)).toEqual([0x7b, 0x22, 0x6b, 0x22, 0x3a, 0x22, 0xc3, 0xa9, 0x22, 0x7d]);
  });

  it('aceita inteiros (inclui negativos, zero, -0 e limites seguros) e bigint', () => {
    expect(canonicalize([0, -0, -1, 42, Number.MAX_SAFE_INTEGER, Number.MIN_SAFE_INTEGER])).toBe(
      '[0,0,-1,42,9007199254740991,-9007199254740991]',
    );
    expect(canonicalize({ big: 123456789012345678901234567890n })).toBe('{"big":123456789012345678901234567890}');
    expect(canonicalize(1791380000000)).toBe('1791380000000');
  });

  it('rejeita float (não inteiro, NaN, Infinity, JsonFloat integral)', () => {
    for (const bad of [1.5, { a: [0.1] }, NaN, Infinity, -Infinity, new JsonFloat('1.0'), { x: new JsonFloat('2e3') }]) {
      try {
        canonicalize(bad);
        expect.unreachable('deveria rejeitar');
      } catch (e) {
        expect(e).toBeInstanceOf(CanonicalError);
        expect((e as CanonicalError).code).toBe('FLOAT_NOT_ALLOWED');
      }
    }
  });

  it('rejeita inteiro inseguro, undefined, tipos não-JSON e surrogate isolado', () => {
    const cases: [unknown, string][] = [
      [2 ** 53, 'UNSAFE_INTEGER'],
      [{ a: undefined }, 'UNDEFINED_VALUE'],
      [new Date(0), 'INVALID_TYPE'],
      [new Map(), 'INVALID_TYPE'],
      ['\uD800', 'INVALID_STRING'],
      ['a\uDC00', 'INVALID_STRING'],
      [{ '\uD83D': 1 }, 'INVALID_STRING'],
    ];
    for (const [v, code] of cases) {
      expect(() => canonicalize(v)).toThrowError(code);
    }
  });

  it('booleans e null', () => {
    expect(canonicalize({ t: true, f: false, n: null })).toBe('{"f":false,"n":null,"t":true}');
  });

  it('é determinístico independentemente da ordem de inserção', () => {
    const a = { schema: 'rd.pop/1', seq: 1, nonce: 'x', device_id: 'd' };
    const b = { device_id: 'd', nonce: 'x', seq: 1, schema: 'rd.pop/1' };
    expect(canonicalize(a)).toBe(canonicalize(b));
  });
});
