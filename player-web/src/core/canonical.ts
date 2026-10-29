/**
 * JSON canônico RDJ1 (RDPP §0) — bytes que são assinados/hasheados.
 *
 * - Objetos com chaves ordenadas por PONTO DE CÓDIGO (não por unidade UTF-16), sem espaços.
 * - Strings com escapes mínimos: \" \\ \b \f \n \r \t; demais controles < 0x20 como \u00xx (hex minúsculo);
 *   não-ASCII sem escape. Surrogate isolado é rejeitado (não é UTF-8 válido).
 * - Somente inteiros (number inteiro seguro ou bigint), booleanos e null. Float é PROIBIDO.
 * - `undefined`, funções, Date, Map etc. são rejeitados (o protocolo exige todas as chaves presentes).
 *
 * Limitação inerente ao JavaScript: um número "1.0" vindo de JSON.parse vira o inteiro 1 e não pode ser
 * distinguido. Por isso os vetores representam floats como {"$float":"1.5"} e o carregador converte para
 * um número não inteiro (rejeitado aqui) ou, quando o valor é integral (ex.: "1.0"), para `JsonFloat`
 * (também rejeitado aqui).
 */

import { utf8 } from './util';

export type CanonicalErrorCode =
  | 'FLOAT_NOT_ALLOWED'
  | 'UNSAFE_INTEGER'
  | 'INVALID_STRING'
  | 'INVALID_TYPE'
  | 'UNDEFINED_VALUE';

export class CanonicalError extends Error {
  constructor(
    public readonly code: CanonicalErrorCode,
    message: string,
  ) {
    super(`${code}: ${message}`);
    this.name = 'CanonicalError';
  }
}

/** Marca um float cujo valor é integral (ex.: 1.0) — existe só para testes/vetores; sempre rejeitado. */
export class JsonFloat {
  constructor(public readonly literal: string) {}
}

export type JsonValue = null | boolean | number | bigint | string | JsonValue[] | { [key: string]: JsonValue };

/** Comparação por ponto de código (equivalente à ordenação de str em Python / RFC 8785 sobre UTF-16 só p/ BMP). */
export function compareCodePoints(a: string, b: string): number {
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    const ca = a.codePointAt(i)!;
    const cb = b.codePointAt(j)!;
    if (ca !== cb) return ca < cb ? -1 : 1;
    i += ca > 0xffff ? 2 : 1;
    j += cb > 0xffff ? 2 : 1;
  }
  const ra = a.length - i;
  const rb = b.length - j;
  return ra === rb ? 0 : ra < rb ? -1 : 1;
}

function escapeString(s: string): string {
  let out = '"';
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c >= 0xd800 && c <= 0xdbff) {
      const next = s.charCodeAt(i + 1);
      if (!(next >= 0xdc00 && next <= 0xdfff)) throw new CanonicalError('INVALID_STRING', 'surrogate alto isolado');
      out += s[i]! + s[i + 1]!;
      i++;
      continue;
    }
    if (c >= 0xdc00 && c <= 0xdfff) throw new CanonicalError('INVALID_STRING', 'surrogate baixo isolado');
    switch (c) {
      case 0x22:
        out += '\\"';
        break;
      case 0x5c:
        out += '\\\\';
        break;
      case 0x08:
        out += '\\b';
        break;
      case 0x0c:
        out += '\\f';
        break;
      case 0x0a:
        out += '\\n';
        break;
      case 0x0d:
        out += '\\r';
        break;
      case 0x09:
        out += '\\t';
        break;
      default:
        if (c < 0x20) out += '\\u' + c.toString(16).padStart(4, '0');
        else out += s[i]!;
    }
  }
  return out + '"';
}

function serialize(v: unknown, path: string): string {
  if (v === null) return 'null';
  switch (typeof v) {
    case 'boolean':
      return v ? 'true' : 'false';
    case 'number':
      if (!Number.isFinite(v) || !Number.isInteger(v)) {
        throw new CanonicalError('FLOAT_NOT_ALLOWED', `número não inteiro em ${path || '$'}`);
      }
      if (!Number.isSafeInteger(v)) throw new CanonicalError('UNSAFE_INTEGER', `inteiro fora do intervalo seguro em ${path || '$'}`);
      return Object.is(v, -0) ? '0' : String(v);
    case 'bigint':
      return v.toString();
    case 'string':
      return escapeString(v);
    case 'undefined':
      throw new CanonicalError('UNDEFINED_VALUE', `valor undefined em ${path || '$'}`);
    case 'object': {
      if (v instanceof JsonFloat) throw new CanonicalError('FLOAT_NOT_ALLOWED', `float ${v.literal} em ${path || '$'}`);
      if (Array.isArray(v)) {
        return '[' + v.map((x, i) => serialize(x, `${path}[${i}]`)).join(',') + ']';
      }
      const proto = Object.getPrototypeOf(v) as unknown;
      if (proto !== Object.prototype && proto !== null) {
        throw new CanonicalError('INVALID_TYPE', `objeto não-JSON em ${path || '$'}`);
      }
      const obj = v as Record<string, unknown>;
      const keys = Object.keys(obj).sort(compareCodePoints);
      const parts: string[] = [];
      for (const k of keys) {
        parts.push(escapeString(k) + ':' + serialize(obj[k], `${path}.${k}`));
      }
      return '{' + parts.join(',') + '}';
    }
    default:
      throw new CanonicalError('INVALID_TYPE', `tipo ${typeof v} em ${path || '$'}`);
  }
}

/** Texto RDJ1 do valor. Lança CanonicalError para floats e tipos não-JSON. */
export function canonicalize(value: unknown): string {
  return serialize(value, '');
}

/** Bytes UTF-8 do texto RDJ1 (o que é assinado). */
export function canonicalBytes(value: unknown): Uint8Array {
  return utf8(canonicalize(value));
}
