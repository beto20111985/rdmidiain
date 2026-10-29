/**
 * Auto-cadastro público do ANUNCIANTE — contrato W-ORG `/api/v1/advertiser-signup`
 * (stack/logs/002/W-ORG_relatorio.md §15). Token do e-mail SOMENTE no header `X-RD-Signup-Token`;
 * rotas públicas sem Bearer/refresh/contexto. `POST /complete` devolve a sessão + cookie de refresh,
 * por isso é enviado com `credentials: 'include'` (sem isso o navegador ignora o Set-Cookie).
 */
import { buildUrl, http, newRequestId, request } from '@/lib/http'
import { toApiError } from '@/lib/errors'

export const SIGNUP_TOKEN_HEADER = 'X-RD-Signup-Token'

export interface PasswordPolicy {
  min_length: number
  max_length: number
  requires_letters_and_digits: boolean
  must_not_contain_email: boolean
}

export interface SignupTerms {
  advertiser_terms: {
    id: string
    kind: string
    version: number
    title: string
    body: string
    body_sha256: string | null
  } | null
  terms_version: string
  privacy_version: string
  password_policy: PasswordPolicy
  captcha_required: boolean
  token_header: string
}

export interface SignupOptions {
  segments: Array<{ id: string; parent_id: string | null; name: string; depth: number }>
  categories: Array<{ id: string; parent_id: string | null; name: string; depth: number; kind: 'SECTOR' | 'CATEGORY' | 'SUBCATEGORY' }>
}

export interface SignupCompleteBody {
  organization: {
    name: string
    trade_name: string | null
    document: string
    document_type: 'CPF' | 'CNPJ'
    responsible_name: string | null
    phone: string
    contact_email: string | null
    address: string | null
    number: string | null
    complement: string | null
    neighborhood: string | null
    city: string
    state: string
    postal_code: string | null
    category_id: string | null
    segment_ids: string[]
  }
  user: { full_name: string; password: string }
  consents: {
    terms_template_id: string
    accept_terms: boolean
    terms_version: string
    privacy_version: string
    accept_privacy: boolean
    marketing_opt_in: boolean
  }
  contact_sales: boolean
  sales_message: string | null
}

export interface SignupCompleteResult {
  status: 'ok'
  access_token: string
  token_type?: string
  expires_in?: number
  user?: { id?: string; name?: string; email?: string }
  organization: { id: string; name: string; business_roles: string[]; document_type: string; kyc_status: string }
  terms_acceptance: { id: string; version: number; pdf_sha256: string | null }
  sales_contact_requested: boolean
  next_steps: string[]
}

const PUBLIC = { auth: false, retryOn401: false, sendContext: false } as const

export const advertiserSignupApi = {
  terms: () => request<SignupTerms>('/advertiser-signup/terms', PUBLIC),
  options: () => request<SignupOptions>('/advertiser-signup/options', PUBLIC),
  start: (email: string, captcha_token?: string) =>
    request<{ status: string; message: string }>('/advertiser-signup/start', {
      ...PUBLIC,
      method: 'POST',
      body: { email, captcha_token },
    }),
  application: (token: string) =>
    request<{ email: string; expires_at: string }>('/advertiser-signup/application', {
      ...PUBLIC,
      headers: { [SIGNUP_TOKEN_HEADER]: token },
    }),
  /** Conclui o cadastro: cria conta + organização + aceites e devolve a sessão (cookie de refresh). */
  complete: async (token: string, body: SignupCompleteBody): Promise<SignupCompleteResult> => {
    const requestId = newRequestId()
    let res: Response
    try {
      res = await fetch(buildUrl('/advertiser-signup/complete'), {
        method: 'POST',
        credentials: 'include',
        headers: {
          Accept: 'application/json',
          'Content-Type': 'application/json',
          'X-Request-ID': requestId,
          [SIGNUP_TOKEN_HEADER]: token,
        },
        body: JSON.stringify(body),
      })
    } catch {
      throw toApiError(0, undefined, { requestId })
    }
    let data: unknown
    try {
      data = await res.json()
    } catch {
      data = undefined
    }
    if (!res.ok) {
      throw toApiError(res.status, data, {
        requestId: res.headers.get('x-request-id') ?? requestId,
        retryAfterHeader: res.headers.get('retry-after'),
      })
    }
    return data as SignupCompleteResult
  },
  /** Já autenticado (sessão devolvida pelo complete): pede contato do comercial. */
  salesContact: (org_id: string, message: string | null) =>
    http.post<{ status: string; already_requested: boolean; message: string }>('/advertiser-signup/sales-contact', {
      org_id,
      message,
    }),
}
