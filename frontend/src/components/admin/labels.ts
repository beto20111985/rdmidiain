/**
 * Rótulos pt-BR e tons de badge para os códigos devolvidos pela API (status sempre com
 * palavra + cor; nunca só cor — DS §10). Código desconhecido → o próprio código.
 */
export type Tone = 'success' | 'info' | 'warning' | 'danger' | 'neutral' | 'outline'

export interface LabelTone {
  label: string
  tone: Tone
}

export function lookup(map: Record<string, LabelTone>, code: string | null | undefined): LabelTone {
  if (!code) return { label: '—', tone: 'neutral' }
  return map[code] ?? { label: code, tone: 'neutral' }
}

export function labelOf(map: Record<string, string>, code: string | null | undefined): string {
  if (!code) return '—'
  return map[code] ?? code
}

// ─── Perfis / escopos ────────────────────────────────────────────────────────
export const ROLE_LABELS: Record<string, string> = {
  super_admin: 'Super Admin',
  admin: 'Admin',
  employee: 'Funcionário',
  financial: 'Financeiro',
  designer: 'Designer',
  sales: 'Comercial',
  technician: 'Técnico',
  establishment_owner: 'Estabelecimento',
  advertiser: 'Anunciante',
  agency: 'Agência',
}

/** Perfis que não podem ser personificados (contrato §2.1; backend PRIVILEGED_ROLES). */
export const IMPERSONATION_BLOCKED_ROLES = ['super_admin', 'admin', 'financial']

export type ScopeType = 'GLOBAL' | 'ORGANIZATION' | 'ESTABLISHMENT' | 'CITY'

export const SCOPE_LABELS: Record<ScopeType, string> = {
  GLOBAL: 'Global',
  ORGANIZATION: 'Empresa',
  ESTABLISHMENT: 'Estabelecimento',
  CITY: 'Cidade',
}

// ─── Organizações ────────────────────────────────────────────────────────────
export const BUSINESS_ROLES = ['ADVERTISER', 'PARTNER', 'AGENCY', 'FRANCHISE'] as const
export type BusinessRole = (typeof BUSINESS_ROLES)[number]

export const BUSINESS_ROLE_LABELS: Record<BusinessRole, string> = {
  ADVERTISER: 'Anunciante',
  PARTNER: 'Parceiro (estabelecimento)',
  AGENCY: 'Agência',
  FRANCHISE: 'Franquia',
}

export const KYC: Record<string, LabelTone> = {
  PENDING: { label: 'KYC pendente', tone: 'warning' },
  APPROVED: { label: 'KYC aprovado', tone: 'success' },
  REJECTED: { label: 'KYC reprovado', tone: 'danger' },
}

export const UFS = [
  'AC', 'AL', 'AM', 'AP', 'BA', 'CE', 'DF', 'ES', 'GO', 'MA', 'MG', 'MS', 'MT', 'PA', 'PB', 'PE', 'PI', 'PR', 'RJ', 'RN',
  'RO', 'RR', 'RS', 'SC', 'SE', 'SP', 'TO',
] as const

// ─── Suporte ─────────────────────────────────────────────────────────────────
export const TICKET_STATUS: Record<string, LabelTone> = {
  ABERTO: { label: 'Aberto', tone: 'info' },
  EM_ATENDIMENTO: { label: 'Em atendimento', tone: 'warning' },
  AGUARDANDO_CLIENTE: { label: 'Aguardando cliente', tone: 'warning' },
  RESOLVIDO: { label: 'Resolvido', tone: 'success' },
  FECHADO: { label: 'Fechado', tone: 'neutral' },
  CANCELADO: { label: 'Cancelado', tone: 'neutral' },
}

export const TICKET_CATEGORY: Record<string, string> = {
  DUVIDA: 'Dúvida',
  PROBLEMA_TELA: 'Problema na tela',
  RETIRADA_ANUNCIO: 'Retirada de anúncio',
  CAMPANHA: 'Campanha',
  FINANCEIRO: 'Financeiro',
  INSTALACAO: 'Instalação',
  MANUTENCAO: 'Manutenção',
  OUTRO: 'Outro',
}

export const TICKET_PRIORITY: Record<string, LabelTone> = {
  BAIXA: { label: 'Baixa', tone: 'neutral' },
  MEDIA: { label: 'Média', tone: 'info' },
  ALTA: { label: 'Alta', tone: 'warning' },
  CRITICA: { label: 'Crítica', tone: 'danger' },
}

/** Transições do backend (support/service.py). */
export const TICKET_TRANSITIONS: Record<string, string[]> = {
  ABERTO: ['EM_ATENDIMENTO', 'AGUARDANDO_CLIENTE', 'RESOLVIDO', 'CANCELADO'],
  EM_ATENDIMENTO: ['AGUARDANDO_CLIENTE', 'RESOLVIDO', 'CANCELADO'],
  AGUARDANDO_CLIENTE: ['EM_ATENDIMENTO', 'RESOLVIDO', 'CANCELADO'],
  RESOLVIDO: ['FECHADO', 'EM_ATENDIMENTO'],
  FECHADO: [],
  CANCELADO: [],
}
export const TICKET_CLIENT_TRANSITIONS: Array<[string, string]> = [
  ['ABERTO', 'CANCELADO'],
  ['RESOLVIDO', 'FECHADO'],
  ['RESOLVIDO', 'EM_ATENDIMENTO'],
]

// ─── Designer ────────────────────────────────────────────────────────────────
export const DESIGN_STATUS: Record<string, LabelTone> = {
  BRIEFING: { label: 'Briefing (aguardando orçamento)', tone: 'info' },
  AGUARDANDO_PAGAMENTO: { label: 'Aguardando pagamento', tone: 'warning' },
  NA_FILA: { label: 'Na fila', tone: 'info' },
  EM_DESIGN: { label: 'Em criação', tone: 'warning' },
  EM_REVISAO: { label: 'Aguardando sua revisão', tone: 'warning' },
  ALTERACAO_SOLICITADA: { label: 'Alteração solicitada', tone: 'warning' },
  APROVADA: { label: 'Aprovada', tone: 'success' },
  PUBLICADA: { label: 'Publicada na biblioteca', tone: 'success' },
  CANCELADA: { label: 'Cancelada', tone: 'neutral' },
}

/** Na visão da equipe RD, "EM_REVISAO" significa "com o cliente". */
export const DESIGN_STATUS_STAFF: Record<string, LabelTone> = {
  ...DESIGN_STATUS,
  EM_REVISAO: { label: 'Em revisão do cliente', tone: 'warning' },
}

export const DESIGN_VERSION_STATUS: Record<string, LabelTone> = {
  EM_REVISAO: { label: 'Em revisão', tone: 'warning' },
  ALTERACAO_SOLICITADA: { label: 'Alteração solicitada', tone: 'warning' },
  APROVADA: { label: 'Aprovada', tone: 'success' },
  SUBSTITUIDA: { label: 'Substituída', tone: 'neutral' },
}

export const DESIGN_KIND: Record<string, string> = {
  ANUNCIO_EXTERNO: 'Anúncio externo',
  CONTEUDO_INTERNO: 'Conteúdo interno do estabelecimento',
}

export const DESIGN_BILLING: Record<string, string> = {
  A_DEFINIR: 'Orçamento a definir',
  COBRAR: 'Cobrado',
  NEGOCIADO: 'Negociado (contrato)',
  ISENTO: 'Isento',
}

export const PAYMENT_STATUS: Record<string, LabelTone> = {
  NAO_APLICAVEL: { label: 'Sem cobrança', tone: 'neutral' },
  PENDENTE: { label: 'Pagamento pendente', tone: 'warning' },
  PAGO: { label: 'Pago', tone: 'success' },
}

export const EXTRA_REVISION_STATUS: Record<string, LabelTone> = {
  SOLICITADA: { label: 'Solicitada', tone: 'info' },
  AGUARDANDO_PAGAMENTO: { label: 'Aguardando pagamento', tone: 'warning' },
  LIBERADA: { label: 'Liberada', tone: 'success' },
  RECUSADA: { label: 'Recusada', tone: 'danger' },
}

export const ATTACHMENT_KIND: Record<string, string> = {
  LOGOTIPO: 'Logotipo',
  IMAGEM: 'Imagem',
  VIDEO: 'Vídeo',
  REFERENCIA: 'Referência',
  TEXTO: 'Texto',
  OUTRO: 'Outro',
}

// ─── Control plane ───────────────────────────────────────────────────────────
export const CP_HEALTH: Record<string, LabelTone> = {
  OK: { label: 'Saudável', tone: 'success' },
  DEGRADADO: { label: 'Degradada', tone: 'warning' },
  FORA_DO_AR: { label: 'Fora do ar', tone: 'danger' },
  SEM_SINAL: { label: 'Sem sinal (heartbeat)', tone: 'danger' },
  DESCONHECIDO: { label: 'Desconhecida', tone: 'neutral' },
}

export const CP_INSTANCE_KIND: Record<string, string> = {
  RD_PRODUCAO: 'RD produção',
  DEV: 'Desenvolvimento (VPS)',
  WHITE_LABEL: 'White-label',
  FRANQUIA: 'Franquia',
  LICENCA: 'Licença',
}

export const CP_INSTANCE_STATUS: Record<string, LabelTone> = {
  ATIVA: { label: 'Ativa', tone: 'success' },
  INATIVA: { label: 'Inativa', tone: 'neutral' },
  MANUTENCAO: { label: 'Em manutenção', tone: 'warning' },
}

export const CP_RELEASE_STATUS: Record<string, LabelTone> = {
  RASCUNHO: { label: 'DEV (rascunho)', tone: 'neutral' },
  VALIDADA: { label: 'Validada', tone: 'info' },
  PUBLICADA: { label: 'Publicada', tone: 'success' },
  DESCONTINUADA: { label: 'Descontinuada', tone: 'neutral' },
}

export const CP_ROLLOUT_STATUS: Record<string, LabelTone> = {
  PENDENTE: { label: 'Pendente', tone: 'info' },
  EM_ANDAMENTO: { label: 'Em andamento', tone: 'warning' },
  PAUSADO: { label: 'Pausado', tone: 'neutral' },
  CONCLUIDO: { label: 'Concluído', tone: 'success' },
  SIMULADO: { label: 'SIMULAÇÃO concluída', tone: 'info' },
  BLOQUEADO_DEPENDENCIA: { label: 'Bloqueado (dependência externa)', tone: 'warning' },
  FALHOU: { label: 'Falhou', tone: 'danger' },
  CANCELADO: { label: 'Cancelado', tone: 'neutral' },
}

export const CP_TARGET_STATUS: Record<string, LabelTone> = {
  NA_FILA: { label: 'Na fila', tone: 'neutral' },
  EM_EXECUCAO: { label: 'Em execução', tone: 'warning' },
  CONCLUIDO: { label: 'Concluído', tone: 'success' },
  SIMULADO: { label: 'SIMULADO', tone: 'info' },
  BLOQUEADO_DEPENDENCIA: { label: 'Bloqueado (dependência externa)', tone: 'warning' },
  FALHOU: { label: 'Falhou', tone: 'danger' },
  CANCELADO: { label: 'Cancelado', tone: 'neutral' },
}

export const CP_TARGET_KIND: Record<string, string> = { UMA: 'Uma instância', GRUPO: 'Grupo', TODAS: 'Todas as instâncias' }

// ─── Notificações / LGPD / configurações ─────────────────────────────────────
export const CHANNEL_LABELS: Record<string, string> = { IN_APP: 'Central', EMAIL: 'E-mail', PUSH: 'Push' }

export const DELIVERY_STATUS: Record<string, LabelTone> = {
  SENT: { label: 'enviado', tone: 'success' },
  PENDING: { label: 'pendente', tone: 'warning' },
  SENDING: { label: 'enviando', tone: 'warning' },
  FAILED: { label: 'falhou', tone: 'danger' },
  UNAVAILABLE: { label: 'indisponível', tone: 'neutral' },
  SKIPPED: { label: 'desativado', tone: 'neutral' },
}

export const DELETION_STATUS: Record<string, LabelTone> = {
  PENDING: { label: 'Pendente', tone: 'warning' },
  COMPLETED: { label: 'Anonimizado', tone: 'success' },
  REJECTED: { label: 'Recusado', tone: 'danger' },
  CANCELLED: { label: 'Cancelado', tone: 'neutral' },
}

export const CONSENT_DOCUMENTS: Record<string, string> = {
  terms: 'Termos de uso',
  privacy: 'Política de privacidade',
  cookies: 'Cookies não essenciais',
  marketing: 'Comunicações de marketing',
}

export const SETTING_CATEGORY_LABELS: Record<string, string> = {
  campanhas: 'Campanhas',
  'inventário': 'Inventário',
  financeiro: 'Financeiro',
  players: 'Players e alertas',
  scheduler: 'Ciclo de exibição',
  'proof-of-play': 'Proof-of-play',
  comercial: 'Comercial (CRM)',
  design: 'Produção de arte',
  suporte: 'Suporte',
  geral: 'Geral',
}
