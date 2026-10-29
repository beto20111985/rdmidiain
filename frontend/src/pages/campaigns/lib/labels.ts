/**
 * Rótulos pt-BR e mapeamentos de status/erros do domínio de campanhas.
 * Status de campanha seguem a máquina de estados oficial (P001 §39) — 12 status em PT +
 * REPRODUZINDO, que NÃO é status: é derivado de comprovação de exibição recente (PoP).
 */
import type { StatusTone } from '@/components/patterns/status-presets'
import type {
  CampaignStatus,
  CreativeStatus,
  Exclusivity,
  FreqKind,
  MediaStatus,
  OrderStatus,
  PeriodType,
  PlanStatus,
  Priority,
  TargetType,
} from './types'

export const CAMPAIGN_STATUS: Record<CampaignStatus, { label: string; tone: StatusTone; hint: string }> = {
  RASCUNHO: { label: 'Rascunho', tone: 'neutral', hint: 'Ainda não enviada para pagamento.' },
  AGUARDANDO_PAGAMENTO: { label: 'Aguardando pagamento', tone: 'warning', hint: 'Cobrança gerada; reserva de inventário ativa.' },
  AGUARDANDO_MIDIA: { label: 'Aguardando mídia', tone: 'warning', hint: 'Paga; envie os anúncios para análise.' },
  EM_ANALISE: { label: 'Em análise', tone: 'warning', hint: 'A equipe RD está revisando a campanha.' },
  APROVADA: { label: 'Aprovada', tone: 'info', hint: 'Aprovada; aguardando o início ou a sincronização.' },
  AGENDADA: { label: 'Agendada', tone: 'info', hint: 'Programação publicada para as telas.' },
  SINCRONIZANDO: { label: 'Sincronizando', tone: 'warning', hint: 'Aguardando as telas confirmarem a nova programação.' },
  ATIVA: { label: 'Ativa', tone: 'success', hint: 'Programação confirmada pelas telas. Exibição comprovada só com registros de reprodução.' },
  PAUSADA: { label: 'Pausada', tone: 'neutral', hint: 'Exibição suspensa.' },
  FINALIZADA: { label: 'Finalizada', tone: 'neutral', hint: 'Período encerrado às 23:59:59 no fuso de cada tela.' },
  REJEITADA: { label: 'Rejeitada', tone: 'danger', hint: 'Corrija conforme o motivo e reenvie.' },
  CANCELADA: { label: 'Cancelada', tone: 'neutral', hint: 'Campanha cancelada.' },
}

export const CAMPAIGN_STATUS_ORDER: CampaignStatus[] = [
  'RASCUNHO',
  'AGUARDANDO_PAGAMENTO',
  'AGUARDANDO_MIDIA',
  'EM_ANALISE',
  'APROVADA',
  'AGENDADA',
  'SINCRONIZANDO',
  'ATIVA',
  'PAUSADA',
  'FINALIZADA',
  'REJEITADA',
  'CANCELADA',
]

export function campaignStatusLabel(s: string): string {
  return (CAMPAIGN_STATUS as Record<string, { label: string }>)[s]?.label ?? s
}

export const CREATIVE_STATUS: Record<CreativeStatus, { label: string; tone: StatusTone }> = {
  RASCUNHO: { label: 'Rascunho', tone: 'neutral' },
  EM_ANALISE: { label: 'Em análise', tone: 'warning' },
  APROVADO: { label: 'Aprovado', tone: 'success' },
  REJEITADO: { label: 'Rejeitado', tone: 'danger' },
  SUBSTITUIDO: { label: 'Substituído', tone: 'neutral' },
  ARQUIVADO: { label: 'Arquivado', tone: 'neutral' },
}

export const TECHNICAL_STATUS: Record<string, { label: string; tone: StatusTone }> = {
  PENDENTE: { label: 'Mídia em processamento', tone: 'warning' },
  VALIDO: { label: 'Mídia válida', tone: 'success' },
  INVALIDO: { label: 'Mídia inválida', tone: 'danger' },
}

export const MEDIA_STATUS: Record<MediaStatus, { label: string; tone: StatusTone }> = {
  UPLOADED: { label: 'Na fila', tone: 'warning' },
  VALIDATING: { label: 'Validando', tone: 'warning' },
  PROCESSING: { label: 'Processando', tone: 'warning' },
  READY: { label: 'Pronta', tone: 'success' },
  REJECTED: { label: 'Rejeitada', tone: 'danger' },
  FAILED: { label: 'Falha no processamento', tone: 'danger' },
}

export const ORDER_STATUS: Record<OrderStatus, { label: string; tone: StatusTone }> = {
  AGUARDANDO_PAGAMENTO: { label: 'Aguardando pagamento', tone: 'warning' },
  PAGO: { label: 'Pago', tone: 'success' },
  EXPIRADO: { label: 'Expirado', tone: 'neutral' },
  CANCELADO: { label: 'Cancelado', tone: 'neutral' },
}

export const PLAN_STATUS: Record<PlanStatus, { label: string; tone: StatusTone }> = {
  RASCUNHO: { label: 'Rascunho', tone: 'neutral' },
  PENDENTE_APROVACAO: { label: 'Aguardando aprovação', tone: 'warning' },
  ATIVO: { label: 'Ativo', tone: 'success' },
  INATIVO: { label: 'Inativo', tone: 'neutral' },
  REJEITADO: { label: 'Rejeitado', tone: 'danger' },
}

export const PERIOD_LABEL: Record<PeriodType, string> = {
  AVULSO: 'Avulso',
  MENSAL: 'Mensal',
  SEMESTRAL: 'Semestral',
  ANUAL: 'Anual',
}

export const FREQ_LABEL: Record<FreqKind, string> = {
  PER_HOUR: 'Por hora',
  PER_DAY: 'Por dia',
  SPECIFIC_TIME: 'Horários específicos',
}

export const PRIORITY_LABEL: Record<Priority, string> = { STANDARD: 'Padrão', PREMIUM: 'Premium (prioridade)' }
export const EXCLUSIVITY_LABEL: Record<Exclusivity, string> = {
  NONE: 'Sem exclusividade',
  COMPETITOR_EXCLUSIVE: 'Exclusividade de concorrente',
}

export const TARGET_LABEL: Record<TargetType, string> = {
  COUNTRY: 'País',
  REGION: 'Região',
  STATE: 'Estado (UF)',
  CITY: 'Cidade',
  NEIGHBORHOOD: 'Bairro',
  POSTAL_CODE: 'CEP (ou prefixo)',
  CATEGORY: 'Categoria de estabelecimento',
  ESTABLISHMENT: 'Estabelecimento específico',
}

export const REGIONS: Array<{ value: string; label: string }> = [
  { value: 'NORTE', label: 'Norte' },
  { value: 'NORDESTE', label: 'Nordeste' },
  { value: 'CENTRO_OESTE', label: 'Centro-Oeste' },
  { value: 'SUDESTE', label: 'Sudeste' },
  { value: 'SUL', label: 'Sul' },
]

export function regionLabel(v: string | null | undefined): string {
  return REGIONS.find((r) => r.value === v)?.label ?? v ?? '—'
}

export const UFS = [
  'AC', 'AL', 'AP', 'AM', 'BA', 'CE', 'DF', 'ES', 'GO', 'MA', 'MT', 'MS', 'MG', 'PA',
  'PB', 'PR', 'PE', 'PI', 'RJ', 'RN', 'RS', 'RO', 'RR', 'SC', 'SP', 'SE', 'TO',
] as const

/** Bit 0 = segunda-feira … bit 6 = domingo (datetime.weekday() do backend). */
export const WEEKDAYS: Array<{ bit: number; short: string; label: string }> = [
  { bit: 0, short: 'Seg', label: 'Segunda-feira' },
  { bit: 1, short: 'Ter', label: 'Terça-feira' },
  { bit: 2, short: 'Qua', label: 'Quarta-feira' },
  { bit: 3, short: 'Qui', label: 'Quinta-feira' },
  { bit: 4, short: 'Sex', label: 'Sexta-feira' },
  { bit: 5, short: 'Sáb', label: 'Sábado' },
  { bit: 6, short: 'Dom', label: 'Domingo' },
]

/** Passos do motor de conflitos (rule_path) → texto. */
export const CONFLICT_REASON: Record<string, string> = {
  PROIBIDO_UNIVERSAL: 'Conteúdo proibido pela política universal (não sobrescrevível)',
  OVERRIDE_ADMIN: 'Override administrativo (campanha × estabelecimento)',
  EXCECAO_ESTABELECIMENTO: 'Exceção do estabelecimento',
  MESMA_REDE: 'Mesma rede (anunciante é a própria rede)',
  MESMO_ANUNCIANTE: 'Mesmo anunciante',
  REGRA_EXPLICITA: 'Regra explícita da matriz',
  CONCORRENTE: 'Concorrência declarada entre empresas',
  MESMO_RAMO: 'Mesmo ramo de atividade',
  SEGMENTO: 'Sobreposição de segmentos',
  SEM_CONFLITO: 'Sem conflito',
}

export function conflictReasonLabel(code: string | null | undefined): string {
  if (!code) return '—'
  return CONFLICT_REASON[code] ?? code
}

export const TRIGGER_LABEL: Record<string, string> = {
  checkout: 'Enviada para pagamento',
  payment_expired: 'Reserva expirou sem pagamento',
  order_cancelled: 'Pedido cancelado',
  payment_confirmed: 'Pagamento confirmado',
  media_submitted: 'Mídia enviada para análise',
  approve: 'Aprovada',
  reject: 'Rejeitada',
  resubmit: 'Reenviada após correção',
  change_requested: 'Alteração solicitada',
  change_rejected: 'Alteração rejeitada (versão anterior mantida)',
  mark_scheduled: 'Programação publicada',
  mark_syncing: 'Sincronizando com as telas',
  ack_active: 'Telas confirmaram a programação',
  pause: 'Pausada',
  kill: 'Interrompida (kill emergencial)',
  resume: 'Retomada',
  expired: 'Período encerrado',
  cancel: 'Cancelada',
  created: 'Criada',
}

export const PRICE_ITEM_KIND_LABEL: Record<string, string> = {
  DURATION_BASE: 'Preço-base por duração',
  FREQUENCY: 'Frequência',
  SCREEN_COUNT: 'Faixa de telas',
  PERIOD: 'Período',
  REGION: 'Região / UF / cidade',
  TIME_BAND: 'Faixa horária',
  SCREEN_CLASS: 'Classe da tela',
  VENUE_CLASS: 'Classe do estabelecimento',
  ESTABLISHMENT: 'Estabelecimento específico',
  EXCLUSIVITY: 'Exclusividade',
  PRIORITY: 'Prioridade / premium',
  MIN_PRICE: 'Preço mínimo',
  FEE: 'Taxa de serviço',
  TAX: 'Impostos',
  COST_GATEWAY: 'Custo do gateway',
  COST_SELLER: 'Comissão do vendedor',
  COST_SERVER: 'Custo de servidor',
  COST_EQUIPMENT: 'Custo de equipamento',
  COST_DESIGNER: 'Custo de designer',
  COST_DIRECT: 'Custos diretos',
}

export const PRICE_ITEM_MODES: Record<string, string[]> = {
  DURATION_BASE: ['BASE_CENTS'],
  FREQUENCY: ['MULTIPLIER_BPS', 'ADD_CENTS'],
  SCREEN_COUNT: ['MULTIPLIER_BPS', 'ADD_CENTS'],
  PERIOD: ['MULTIPLIER_BPS', 'ADD_CENTS'],
  REGION: ['MULTIPLIER_BPS', 'ADD_CENTS'],
  TIME_BAND: ['MULTIPLIER_BPS'],
  SCREEN_CLASS: ['MULTIPLIER_BPS', 'ADD_CENTS'],
  VENUE_CLASS: ['MULTIPLIER_BPS', 'ADD_CENTS'],
  ESTABLISHMENT: ['MULTIPLIER_BPS'],
  EXCLUSIVITY: ['MULTIPLIER_BPS', 'ADD_CENTS'],
  PRIORITY: ['MULTIPLIER_BPS', 'ADD_CENTS'],
  MIN_PRICE: ['FIXED_CENTS'],
  FEE: ['FIXED_CENTS'],
  TAX: ['PERCENT_BPS'],
  COST_GATEWAY: ['PERCENT_BPS', 'FIXED_CENTS'],
  COST_SELLER: ['PERCENT_BPS'],
  COST_SERVER: ['PER_SCREEN_MONTH_CENTS'],
  COST_EQUIPMENT: ['PER_SCREEN_MONTH_CENTS'],
  COST_DESIGNER: ['FIXED_CENTS'],
  COST_DIRECT: ['FIXED_CENTS', 'PERCENT_BPS'],
}

export const PRICE_MODE_LABEL: Record<string, string> = {
  BASE_CENTS: 'Valor base (centavos por inserção por tela)',
  MULTIPLIER_BPS: 'Multiplicador (10000 = ×1,0)',
  ADD_CENTS: 'Acréscimo por inserção (centavos)',
  FIXED_CENTS: 'Valor fixo (centavos)',
  PERCENT_BPS: 'Percentual (bps; 100 = 1%)',
  PER_SCREEN_MONTH_CENTS: 'Por tela por mês (centavos)',
}

/** Chave sugerida por tipo de item (o backend valida). */
export const PRICE_ITEM_KEY_HINT: Record<string, string> = {
  DURATION_BASE: '30, 60 ou 120',
  FREQUENCY: 'PER_HOUR, PER_DAY, SPECIFIC_TIME ou *',
  PERIOD: 'AVULSO, MENSAL, SEMESTRAL, ANUAL ou *',
  REGION: 'Região (SUDESTE), UF (GO) ou cidade (GO:goiania)',
  SCREEN_CLASS: 'STANDARD ou PREMIUM',
  VENUE_CLASS: 'STANDARD ou PREMIUM',
  ESTABLISHMENT: 'ID do estabelecimento',
  EXCLUSIVITY: 'COMPETITOR_EXCLUSIVE',
  PRIORITY: 'PREMIUM',
  FEE: 'ART, INSTALLATION ou UPGRADE',
}

export const HOLD_STATUS: Record<string, { label: string; tone: StatusTone }> = {
  HELD: { label: 'Reservada', tone: 'warning' },
  CONVERTED: { label: 'Convertida (paga)', tone: 'success' },
  EXPIRED: { label: 'Expirada', tone: 'neutral' },
  CANCELLED: { label: 'Liberada', tone: 'neutral' },
  PAID_WITHOUT_INVENTORY: { label: 'Paga sem inventário', tone: 'danger' },
}

/** Nível da árvore de taxonomia (setor › categoria › subcategoria). */
export const TAXONOMY_KIND_LABEL: Record<string, string> = { SECTOR: 'Setor', CATEGORY: 'Categoria', SUBCATEGORY: 'Subcategoria' }

// ─── Disponibilidade: por que telas ficaram de fora (SOMENTE equipe RD — AÇÃO 004) ────────────────────────
interface ExclusionText {
  /** Motivo em linguagem simples (continua a frase "… fora do inventário: "). */
  why: string
  /** Próximo passo (continua "Próximo passo: "). */
  next: string
}

/** Motivos de tela FORA do inventário vendável (backend: ``targeting.out_of_inventory_reason``). */
export const OUT_OF_INVENTORY_TEXT: Record<string, ExclusionText> = {
  AWAITING_CONTRACT: {
    why: 'estabelecimento aguardando o aceite do contrato de parceria',
    next: 'registre o aceite em Estabelecimentos → aba “Contratos e comodato”',
  },
  ESTABLISHMENT_INACTIVE: { why: 'estabelecimento inativo ou excluído', next: 'reative o estabelecimento em Estabelecimentos' },
  SCREEN_INACTIVE: { why: 'tela com status “Inativa”', next: 'altere o status da tela para “Ativa” em Telas → Status' },
  NOT_SELLABLE: {
    why: 'tela marcada como fora do inventário',
    next: 'confira a tela em Telas (filtro “Vendável”: Fora do inventário)',
  },
}

/** Motivos de conflito (``reason_code`` do motor) para o aviso da disponibilidade. */
export const CONFLICT_EXCLUSION_TEXT: Record<string, ExclusionText> = {
  SEGMENTO: {
    why: 'conflito de segmento (o local vende o mesmo segmento do anúncio)',
    next: 'ajuste os segmentos do anúncio (etapa 1) ou crie uma exceção em Taxonomia e conflitos → Exceções',
  },
  MESMO_RAMO: {
    why: 'conflito de ramo (o local é do mesmo ramo de atividade do anunciante)',
    next: 'se for permitido, crie uma exceção em Taxonomia e conflitos → Exceções',
  },
  CONCORRENTE: { why: 'concorrência declarada entre o local e o anunciante', next: 'revise os concorrentes declarados em Taxonomia e conflitos' },
  PROIBIDO_UNIVERSAL: {
    why: 'conteúdo proibido pela política universal',
    next: 'esse bloqueio não pode ser contornado; revise os segmentos do anúncio (etapa 1)',
  },
  REGRA_EXPLICITA: { why: 'regra explícita da matriz de conflitos', next: 'revise a regra em Taxonomia e conflitos' },
}

export function conflictExclusionText(code: string): ExclusionText {
  return CONFLICT_EXCLUSION_TEXT[code] ?? { why: `conflito (${conflictReasonLabel(code)})`, next: 'veja o detalhe em “Conflitos por estabelecimento”' }
}

/** Aviso ao lado do estabelecimento no seletor da RD quando as telas dele estão fora do inventário. */
export function establishmentInventoryHint(e: { is_active?: boolean; onboarding_status?: string | null }): string | null {
  if (e.is_active === false) return `Fora do inventário: ${OUT_OF_INVENTORY_TEXT.ESTABLISHMENT_INACTIVE.why}`
  if (e.onboarding_status === 'AWAITING_CONTRACT') return 'Fora do inventário: aguardando o aceite do contrato de parceria'
  return null
}
