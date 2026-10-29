/** Tipos das respostas de `/design` e `/templates` (W-BIZ). */
export interface DesignRequest {
  id: string
  code: string
  org_id: string
  establishment_id: string | null
  kind: 'ANUNCIO_EXTERNO' | 'CONTEUDO_INTERNO' | string
  title: string
  briefing: string
  texts: string[]
  references: string[]
  orientation: 'HORIZONTAL' | 'VERTICAL'
  media_type: 'IMAGEM' | 'VIDEO'
  duration_s: number
  status: string
  requested_by: string
  assigned_to: string | null
  billing: string
  price_cents: number
  payment_status: string
  payment_order_id: string | null
  revisions_included: number
  /** Revisões extras LIBERADAS (inteiro). `extra_revisions` é alias deprecado do mesmo valor. */
  extra_revisions_count: number
  revisions_used: number
  revisions_remaining: number
  approved_version_id: string | null
  approved_at: string | null
  published_media_id: string | null
  published_at: string | null
  due_at: string | null
  cancel_reason: string | null
  version: number
  created_at: string
  updated_at: string | null
}

export interface DesignVersion {
  id: string
  number: number
  source: 'UPLOAD' | 'TEMPLATE' | string
  template_id: string | null
  filename: string
  content_type: string
  size_bytes: number
  sha256: string
  width: number | null
  height: number | null
  notes: string | null
  status: string
  locked: boolean
  created_by: string
  created_at: string
}

export interface DesignApproval {
  id: string
  version_id: string
  decision: 'APROVADA' | 'ALTERACAO_SOLICITADA' | string
  comment: string | null
  user_id: string
  ip_address: string | null
  user_agent: string | null
  version_sha256: string
  revision_number: number | null
  decided_at: string
}

export interface ExtraRevisionPurchase {
  id: string
  status: string
  price_cents: number
  reason: string | null
  payment_order_id: string | null
  created_at: string
  decided_at: string | null
  released_at: string | null
}

export interface DesignRequestDetail extends DesignRequest {
  versions: DesignVersion[]
  attachments: Array<{ id: string; kind: string; filename: string; content_type: string; size_bytes: number; sha256: string; created_at: string }>
  approvals: DesignApproval[]
  history: Array<{ from: string | null; to: string; note: string | null; at: string }>
  /** Pedidos/compras de revisão extra (lista). */
  extra_revision_purchases: ExtraRevisionPurchase[]
}

export interface DesignComment {
  id: string
  version_id: string | null
  /** null quando o autor é da equipe RD e quem vê é o cliente (o servidor oculta o id). */
  author_id: string | null
  body: string
  is_internal: boolean
  created_at: string
  author_name?: string | null
  author_is_staff?: boolean | null
}

export interface DesignQueue {
  items: DesignRequest[]
  counts: Record<string, number>
  unassigned: number
}

export interface TemplateMeta {
  grid: number
  canvases: Record<'HORIZONTAL' | 'VERTICAL', { width: number; height: number }>
  fonts: string[]
  layer_types: string[]
  limits: { layers: number; variables: number; font_px: [number, number]; asset_mb: number }
}

export interface DesignTemplateRow {
  id: string
  name: string
  description: string | null
  orientation: 'HORIZONTAL' | 'VERTICAL'
  width: number
  height: number
  spec: unknown
  status: 'RASCUNHO' | 'ATIVO' | 'ARQUIVADO' | string
  version: number
  org_id: string | null
  created_at: string | null
  updated_at: string | null
}
