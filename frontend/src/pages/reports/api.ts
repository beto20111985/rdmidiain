/**
 * Relatórios, exportações e comprovantes de veiculação (W-BIZ `/reports`).
 * Público do relatório (EQUIPE / ANUNCIANTE / ESTABELECIMENTO) é decidido pelo SERVIDOR a
 * partir das permissões; o frontend só informa o contexto (`audience`, `org_id`).
 * "Exibições válidas" = proof-of-play válido (100% da duração) — nunca a programação planejada.
 */
import { http, type Paginated } from '@/lib/http'

export type ReportAudience = 'EQUIPE' | 'ANUNCIANTE' | 'ESTABELECIMENTO'
export type ContentKind = 'EXTERNAL' | 'INTERNAL' | 'INSTITUTIONAL' | 'FALLBACK'

export interface ReportCatalogItem {
  key: string
  label: string
}

export interface ReportColumn {
  key: string
  label: string
  kind: 'text' | 'int' | 'money' | 'date' | 'datetime' | 'bps' | string
}

export interface ReportData {
  title: string
  audience: ReportAudience
  columns: ReportColumn[]
  rows: Array<Record<string, string | number | null>>
  meta: Array<{ label: string; value: string }>
  notes: string[]
  total: number
}

export interface ReportParams {
  start?: string
  end?: string
  audience?: ReportAudience
  org_id?: string
  campaign_id?: string
  screen_id?: string
  kind?: ContentKind
  group_by?: string
}

export interface ReportExport {
  id: string
  report_key: string
  format: 'csv' | 'xlsx' | 'pdf'
  status: string
  filename: string | null
  size_bytes: number | null
  sha256: string | null
  row_count: number | null
  emailed_to: number | null
  params: Record<string, string>
  created_at: string
  expires_at: string | null
  download_url: string
}

export interface DeliveryProof {
  id: string
  code: string
  campaign_id: string
  org_id: string
  period_start: string
  period_end: string
  total_valid_plays: number
  source: string
  pdf_sha256: string
  payload_sha256: string
  kid: string
  created_at: string
  pdf_url: string
}

export interface ProofVerification {
  code: string
  issued_at: string | null
  payload_sha256: string
  payload_signature: string
  pdf_sha256: string
  pdf_signature: string
  kid: string
  alg: string
  public_keys_url: string
  note: string
}

export interface ProofVerifyResult {
  valid: boolean
  reason?: string
  code?: string
  issued_at?: string | null
  period?: { start: string; end: string; timezone_basis?: string } | null
  total_valid_plays?: number
  kid?: string
  pdf_sha256: string
}

type Q = Record<string, string | number | boolean | undefined | null>

export const reportsApi = {
  catalog: (q: { audience?: ReportAudience; org_id?: string }) =>
    http.get<{ audience: ReportAudience; items: ReportCatalogItem[] }>('/reports/catalog', { query: q }),
  data: (key: string, p: ReportParams) => http.get<ReportData>(`/reports/data/${encodeURIComponent(key)}`, { query: p as Q }),
  createExport: (body: ReportParams & { report_key: string; format: 'csv' | 'xlsx' | 'pdf'; email_to_me?: boolean }) =>
    http.post<ReportExport>('/reports/exports', body),
  exports: (q: Q) => http.get<Paginated<ReportExport>>('/reports/exports', { query: q }),
  proofs: (q: { campaign_id?: string }) => http.get<Paginated<DeliveryProof>>('/reports/proofs', { query: q }),
  createProof: (body: { campaign_id: string; start?: string; end?: string }) => http.post<DeliveryProof>('/reports/proofs', body),
  verification: (code: string) =>
    http.get<ProofVerification>(`/reports/proofs/${encodeURIComponent(code.trim().toUpperCase())}/verification`),
  verifyPdf: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return http.post<ProofVerifyResult>('/reports/proofs/verify', fd)
  },
}

/** Caminho relativo à base /api/v1 a partir de uma URL absoluta do backend ("/api/v1/..."). */
export function apiPath(url: string): string {
  return url.startsWith('/api/v1') ? url.slice('/api/v1'.length) : url
}

/** Soma uma coluna inteira das linhas (ex.: total de exibições válidas no período). */
export function sumColumn(rows: ReportData['rows'] | undefined, key: string): number | null {
  if (!rows) return null
  let total = 0
  for (const r of rows) {
    const v = r[key]
    if (typeof v === 'number' && Number.isFinite(v)) total += v
  }
  return total
}
