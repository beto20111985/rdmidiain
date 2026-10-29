/**
 * Mapeia erros do envelope (contrato v1) para campos do formulário:
 * - 422 `details.errors[{loc,msg}]` → `fieldErrors` (fundação);
 * - códigos de domínio conhecidos (ex.: TIMEZONE_INVALID) → campo correspondente.
 * Retorna a mensagem geral quando nenhum campo foi associado (exibida em <Alert>).
 */
import type { FieldValues, Path, UseFormSetError } from 'react-hook-form'
import { errorMessage, isApiError } from '@/lib/errors'

export function applyServerErrors<T extends FieldValues>(
  err: unknown,
  setError: UseFormSetError<T>,
  opts: { fields: readonly string[]; codeToField?: Record<string, string> } = { fields: [] },
): string | null {
  if (!isApiError(err)) return errorMessage(err)
  let mapped = false
  const fe = err.fieldErrors
  for (const [k, msg] of Object.entries(fe)) {
    const key = k.split('.').pop() ?? k
    const target = opts.fields.includes(k) ? k : opts.fields.includes(key) ? key : null
    if (target) {
      setError(target as Path<T>, { type: 'server', message: msg })
      mapped = true
    }
  }
  const byCode = opts.codeToField?.[err.code]
  if (byCode && opts.fields.includes(byCode)) {
    setError(byCode as Path<T>, { type: 'server', message: err.message })
    mapped = true
  }
  if (mapped && err.status === 422) return 'Revise os campos destacados.'
  return mapped ? null : err.message
}
