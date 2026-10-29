/** Validações puras das etapas do onboarding. */

export function docError(doc: string, type: string | null, required: boolean): string | null {
  const d = doc.replace(/[^0-9A-Za-z]/g, '')
  if (!d) return required ? 'Informe o CPF ou CNPJ.' : null
  if (type === 'CPF' && !/^\d{11}$/.test(d)) return 'CPF deve ter 11 dígitos.'
  if (type === 'CNPJ' && !/^[0-9A-Z]{12}\d{2}$/i.test(d)) return 'CNPJ deve ter 14 caracteres.'
  if (!type) return 'Escolha CPF ou CNPJ.'
  return null
}

export const onboardingStatusTone = (s: string): 'warning' | 'success' | 'danger' | 'info' =>
  s === 'SUBMITTED' ? 'warning' : s === 'APPROVED' ? 'success' : s === 'REJECTED' || s === 'CANCELLED' ? 'danger' : 'info'

/** Tipos e limite aceitos pelo backend para documentos (magic bytes: PDF/JPEG/PNG, 10 MB). */
export const DOC_ACCEPT = ['application/pdf', 'image/jpeg', 'image/png']
export const DOC_MAX_BYTES = 10 * 1024 * 1024
