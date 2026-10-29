/**
 * Rótulos pt-BR e tons (DS §10 — palavra + cor + ícone) dos estados vindos da API
 * financeira (W-FIN), comercial (W-BIZ CRM) e de campanhas. Valor desconhecido → o
 * próprio código, em tom neutro (nunca some da tela).
 */
import type { StatusTone } from '@/components/patterns/status-presets'

export interface StatusEntry {
  label: string
  tone: StatusTone
}

export type StatusMap = Record<string, StatusEntry>

export function statusEntry(map: StatusMap, value: string | null | undefined): StatusEntry {
  if (!value) return { label: '—', tone: 'neutral' }
  return map[value] ?? { label: value, tone: 'neutral' }
}

export const PAYMENT_STATUS: StatusMap = {
  CREATED: { label: 'Gerando cobrança', tone: 'warning' },
  PENDING: { label: 'Aguardando pagamento', tone: 'warning' },
  CREATION_FAILED: { label: 'Falha ao gerar cobrança', tone: 'danger' },
  CREATION_UNKNOWN: { label: 'Em verificação', tone: 'warning' },
  PAID: { label: 'Paga', tone: 'success' },
  PARTIALLY_REFUNDED: { label: 'Reembolsada parcialmente', tone: 'info' },
  REFUNDED: { label: 'Reembolsada', tone: 'neutral' },
  FAILED: { label: 'Falhou', tone: 'danger' },
  CANCELLED: { label: 'Cancelada', tone: 'neutral' },
  EXPIRED: { label: 'Expirada', tone: 'neutral' },
}

export const PAYMENT_METHOD: Record<string, string> = {
  PIX: 'PIX',
  pix: 'PIX',
  CREDIT_CARD: 'Cartão de crédito',
  credit_card: 'Cartão de crédito',
  BOLETO: 'Boleto',
  boleto: 'Boleto',
}

export const WITHDRAWAL_STATUS: StatusMap = {
  REQUESTED: { label: 'Solicitado', tone: 'warning' },
  EXECUTING: { label: 'Em execução', tone: 'warning' },
  PROCESSING: { label: 'Processando no banco', tone: 'warning' },
  COMPLETED: { label: 'Pago', tone: 'success' },
  FAILED: { label: 'Falhou', tone: 'danger' },
  CANCELLED: { label: 'Cancelado', tone: 'neutral' },
  UNKNOWN: { label: 'Em conferência manual', tone: 'warning' },
}

export const WITHDRAWAL_KIND: Record<string, string> = {
  ESTABLISHMENT: 'Saque de estabelecimento',
  RD: 'Retirada da RD (PIX OUT)',
  RD_WITHDRAWAL: 'Retirada da RD (PIX OUT)',
  REFUND: 'Reembolso (PIX OUT)',
  PAYABLE: 'Pagamento de conta (PIX OUT)',
}

export const WITHDRAWAL_ORIGIN: Record<string, string> = {
  MANUAL: 'Manual',
  AUTO: 'Automático (dia 1)',
  AUTOMATIC: 'Automático (dia 1)',
}

export const APPROVAL_STATUS: StatusMap = {
  PENDING: { label: 'Pendente', tone: 'warning' },
  APPROVED: { label: 'Aprovado', tone: 'success' },
  REJECTED: { label: 'Rejeitado', tone: 'danger' },
  CANCELLED: { label: 'Cancelado', tone: 'neutral' },
  EXPIRED: { label: 'Expirado', tone: 'neutral' },
}

export const EXECUTION_STATE: StatusMap = {
  NOT_STARTED: { label: 'Não iniciada', tone: 'neutral' },
  PENDING: { label: 'Aguardando execução', tone: 'warning' },
  EXECUTING: { label: 'Executando', tone: 'warning' },
  EXECUTED: { label: 'Executada', tone: 'success' },
  FAILED: { label: 'Falhou', tone: 'danger' },
  UNKNOWN: { label: 'Resultado desconhecido — conciliação manual', tone: 'warning' },
  BLOCKED_EXTERNAL: { label: 'BLOQUEADO POR DEPENDÊNCIA EXTERNA', tone: 'danger' },
}

export const PIX_KEY_STATUS: StatusMap = {
  ACTIVE: { label: 'Ativa', tone: 'success' },
  COOLDOWN: { label: 'Em período de segurança', tone: 'warning' },
  REVOKED: { label: 'Revogada', tone: 'neutral' },
}

export const PIX_KEY_TYPES: Array<{ value: 'cpf' | 'cnpj' | 'email' | 'phone' | 'random'; label: string }> = [
  { value: 'cpf', label: 'CPF' },
  { value: 'cnpj', label: 'CNPJ' },
  { value: 'email', label: 'E-mail' },
  { value: 'phone', label: 'Telefone' },
  { value: 'random', label: 'Chave aleatória' },
]

export function pixKeyTypeLabel(t: string | null | undefined): string {
  return PIX_KEY_TYPES.find((k) => k.value === t)?.label ?? (t || '—')
}

export const PAYABLE_STATUS: StatusMap = {
  OPEN: { label: 'Em aberto', tone: 'warning' },
  PAID: { label: 'Paga', tone: 'success' },
  CANCELLED: { label: 'Cancelada', tone: 'neutral' },
}

export const RECON_STATUS: StatusMap = {
  DIVERGENT: { label: 'Divergente', tone: 'danger' },
  RESOLVED: { label: 'Resolvido', tone: 'success' },
}

export const RECON_RUN_STATUS: StatusMap = {
  RUNNING: { label: 'Em execução', tone: 'warning' },
  COMPLETED: { label: 'Concluída', tone: 'success' },
  FAILED: { label: 'Falhou', tone: 'danger' },
  PROVIDER_UNAVAILABLE: { label: 'Provedor indisponível', tone: 'danger' },
}

export const REFUND_STATUS: StatusMap = {
  AWAITING_APPROVAL: { label: 'Aguardando aprovação', tone: 'warning' },
  APPROVED: { label: 'Aprovado', tone: 'info' },
  PAYING: { label: 'Pagando', tone: 'warning' },
  PAID: { label: 'Pago', tone: 'success' },
  BLOCKED_EXTERNAL_DEPENDENCY: { label: 'BLOQUEADO POR DEPENDÊNCIA EXTERNA', tone: 'danger' },
  REJECTED: { label: 'Rejeitado', tone: 'danger' },
  FAILED: { label: 'Falhou', tone: 'danger' },
  UNKNOWN: { label: 'Em conferência', tone: 'warning' },
}

export const COMMISSION_STATUS: StatusMap = {
  PENDENTE: { label: 'Pendente (competência aberta)', tone: 'warning' },
  FECHADO: { label: 'Fechada', tone: 'success' },
}

/** Tipo do termo de comissão por pagamento (W-FIN 0006). */
export const COMMISSION_TERM_KIND_LABEL: Record<string, string> = {
  INITIAL: 'Contratação inicial',
  RENEWAL: 'Renovação',
  CHANGE: 'Alteração de contrato',
}

export const COMMISSION_TERM_STATUS: StatusMap = {
  ACTIVE: { label: 'Vigente', tone: 'success' },
  CANCELLED: { label: 'Cancelado', tone: 'danger' },
}

export const COMMISSION_MODEL_LABEL: Record<string, string> = {
  A_POOL: 'A — pool fixo',
  B_ATTRIBUTED: 'B — receita atribuída',
}

export const ATTRIBUTION_STATUS: StatusMap = {
  PENDENTE: { label: 'Pendente (aguardando pagamento)', tone: 'warning' },
  APURADA: { label: 'Apurada', tone: 'info' },
  PAGA: { label: 'Paga', tone: 'success' },
  CANCELADA: { label: 'Cancelada', tone: 'neutral' },
}

export const PROPOSAL_STATUS: StatusMap = {
  RASCUNHO: { label: 'Rascunho', tone: 'neutral' },
  AGUARDANDO_APROVACAO: { label: 'Aguardando aprovação do desconto', tone: 'warning' },
  APROVADA: { label: 'Aprovada (pronta para envio)', tone: 'info' },
  ENVIADA: { label: 'Enviada ao cliente', tone: 'info' },
  ACEITA: { label: 'Aceita pelo cliente', tone: 'success' },
  RECUSADA: { label: 'Recusada pelo cliente', tone: 'danger' },
  EXPIRADA: { label: 'Vencida', tone: 'neutral' },
  CONVERTIDA: { label: 'Convertida em pedido', tone: 'success' },
  CANCELADA: { label: 'Cancelada', tone: 'neutral' },
}

export const LEAD_STAGES: Array<{ value: string; label: string }> = [
  { value: 'NOVO', label: 'Novo' },
  { value: 'CONTATO', label: 'Em contato' },
  { value: 'QUALIFICADO', label: 'Qualificado' },
  { value: 'PROPOSTA', label: 'Proposta' },
  { value: 'NEGOCIACAO', label: 'Negociação' },
  { value: 'GANHO', label: 'Ganho' },
  { value: 'PERDIDO', label: 'Perdido' },
]

export const LEAD_STAGE_STATUS: StatusMap = {
  NOVO: { label: 'Novo', tone: 'info' },
  CONTATO: { label: 'Em contato', tone: 'info' },
  QUALIFICADO: { label: 'Qualificado', tone: 'info' },
  PROPOSTA: { label: 'Proposta', tone: 'warning' },
  NEGOCIACAO: { label: 'Negociação', tone: 'warning' },
  GANHO: { label: 'Ganho', tone: 'success' },
  PERDIDO: { label: 'Perdido', tone: 'danger' },
}

/** Transições permitidas (espelho do backend `LEAD_TRANSITIONS`; o servidor revalida — 409). */
export const LEAD_TRANSITIONS: Record<string, string[]> = {
  NOVO: ['CONTATO', 'QUALIFICADO', 'PROPOSTA', 'PERDIDO'],
  CONTATO: ['QUALIFICADO', 'PROPOSTA', 'NEGOCIACAO', 'PERDIDO'],
  QUALIFICADO: ['PROPOSTA', 'NEGOCIACAO', 'PERDIDO', 'CONTATO'],
  PROPOSTA: ['NEGOCIACAO', 'GANHO', 'PERDIDO', 'QUALIFICADO'],
  NEGOCIACAO: ['PROPOSTA', 'GANHO', 'PERDIDO'],
  GANHO: [],
  PERDIDO: ['CONTATO'],
}

export function leadStageLabel(s: string | null | undefined): string {
  return LEAD_STAGES.find((x) => x.value === s)?.label ?? (s || '—')
}

export const ACTIVITY_KINDS: Array<{ value: string; label: string }> = [
  { value: 'NOTA', label: 'Nota' },
  { value: 'LIGACAO', label: 'Ligação' },
  { value: 'REUNIAO', label: 'Reunião' },
  { value: 'EMAIL', label: 'E-mail' },
  { value: 'WHATSAPP', label: 'WhatsApp' },
  { value: 'VISITA', label: 'Visita' },
]

export const CAMPAIGN_STATUS: StatusMap = {
  RASCUNHO: { label: 'Rascunho', tone: 'neutral' },
  AGUARDANDO_PAGAMENTO: { label: 'Aguardando pagamento', tone: 'warning' },
  AGUARDANDO_MIDIA: { label: 'Aguardando mídia', tone: 'warning' },
  EM_ANALISE: { label: 'Em análise', tone: 'warning' },
  APROVADA: { label: 'Aprovada', tone: 'info' },
  AGENDADA: { label: 'Agendada', tone: 'info' },
  SINCRONIZANDO: { label: 'Sincronizando', tone: 'warning' },
  ATIVA: { label: 'Ativa', tone: 'success' },
  PAUSADA: { label: 'Pausada', tone: 'neutral' },
  FINALIZADA: { label: 'Encerrada', tone: 'neutral' },
  REJEITADA: { label: 'Rejeitada', tone: 'danger' },
  CANCELADA: { label: 'Cancelada', tone: 'neutral' },
}

export const SALES_REP_KIND: Record<string, string> = {
  VENDEDOR: 'Vendedor',
  REPRESENTANTE: 'Representante',
  FRANQUIA: 'Franquia',
  GERENTE: 'Gerente',
}

export const COMPENSATION: Record<string, string> = {
  COMISSAO: 'Comissão',
  SALARIO_FIXO: 'Salário fixo',
  AMBOS: 'Salário + comissão',
}

export const DRE_SIGN_NOTE = 'Receitas positivas; deduções e despesas negativas.'

export const ACTION_TYPE_LABEL: Record<string, string> = {
  RD_WITHDRAWAL: 'Retirada (PIX OUT) da RD',
  REFUND_PIX_OUT: 'Reembolso ao anunciante',
  EXPENSE_CREATE: 'Despesa manual',
  EXPENSE_UPDATE: 'Alteração de despesa',
  PAYABLE_SETTLEMENT: 'Pagamento de conta a pagar',
  LEDGER_ADJUSTMENT: 'Ajuste/crédito manual',
  COMMISSION_ADJUSTMENT: 'Ajuste de comissão',
  COMMISSION_EXCEPTION: 'Exceção de comissão de estabelecimento',
  SETTING_CHANGE: 'Alteração de configuração financeira',
  RECONCILIATION_ADJUSTMENT: 'Resolução de conciliação',
  DEPOSIT_REFUND: 'Devolução de caução do parceiro',
  APPROVAL_MODE_CHANGE: 'Mudança do modo de aprovação financeira',
}

/** Rótulos dos campos do payload de aprovação (detalhe revisado antes de aprovar). */
export const PAYLOAD_FIELD_LABEL: Record<string, string> = {
  amount_cents: 'Valor',
  purpose: 'Finalidade',
  pix_key_type: 'Tipo de chave PIX',
  pix_key_masked: 'Chave PIX (mascarada)',
  refund_id: 'Reembolso',
  await_provider_refund: 'Aguardar estorno do provedor',
  description: 'Descrição',
  category: 'Categoria',
  competence_date: 'Competência',
  due_date: 'Vencimento',
  supplier_name: 'Fornecedor',
  supplier_document: 'Documento do fornecedor',
  payable_id: 'Conta a pagar',
  method: 'Forma de pagamento',
  legs: 'Partidas',
  item_id: 'Item de conciliação',
  withdrawal_resolution: 'Desfecho do saque',
  establishment_id: 'Estabelecimento',
  rate_bps: 'Taxa',
  weight_milli: 'Peso',
  receives_commission: 'Recebe comissão',
  auto_payout: 'Pagamento automático',
  valid_from: 'Válido a partir de',
  valid_to: 'Válido até',
  key: 'Configuração',
  value: 'Novo valor',
  expected_version: 'Versão esperada',
  target_mode: 'Novo modo de aprovação',
  from_mode: 'Modo atual',
  refund_cents: 'Valor a devolver',
  retained_cents: 'Valor retido',
  damage_description: 'Avarias/descontos',
  available_cents: 'Disponível',
  payment_order_id: 'Cobrança',
}

export const PROVIDER_CAPABILITY_LABEL: Record<string, string> = {
  identity: 'Identificação da conta',
  balance: 'Consulta de saldo',
  charge_pix: 'Cobrança PIX',
  charge_credit_card: 'Cobrança no cartão de crédito',
  charge_boleto: 'Boleto',
  charge_query: 'Consulta de cobrança',
  charge_list: 'Listagem de cobranças',
  charge_refund: 'Estorno de cobrança',
  charge_cancel: 'Cancelamento de cobrança',
  chargeback_dispute: 'Contestação (chargeback)',
  withdrawal: 'Saque PIX (PIX OUT)',
  withdrawal_simulate: 'Simulação de taxa de saque',
  withdrawal_query: 'Consulta de saque',
  withdrawal_copypaste: 'Saque por PIX copia-e-cola',
  withdrawal_idempotency: 'Idempotência de saque no provedor',
  balance_block: 'Bloqueio de saldo',
  pix_key_registry: 'Consulta de titularidade da chave PIX',
  webhook_withdrawal_format: 'Webhook de saque (formato documentado)',
}

export const RECON_KIND_LABEL: Record<string, string> = {
  BALANCE_MISMATCH: 'Saldo do provedor diferente do caixa interno',
  CHARGE_AMOUNT_MISMATCH: 'Valor pago diferente do cobrado',
  CHARGE_CREATION_UNKNOWN: 'Criação de cobrança sem confirmação',
  CHARGE_DISPUTED: 'Cobrança contestada (chargeback)',
  CHARGE_FEE_INVALID: 'Taxa da cobrança inválida',
  EXTERNAL_REFUND: 'Estorno feito fora do sistema',
  PAID_AFTER_EXPIRY: 'Pagamento após o vencimento',
  PAID_WITHOUT_SERVICE: 'Pagamento sem serviço vinculado',
  PAYMENT_CALLBACK_FAILED: 'Falha ao processar pagamento confirmado',
  WEBHOOK_UNMATCHED: 'Webhook sem cobrança correspondente',
  WITHDRAWAL_NET_MISMATCH: 'Valor líquido de saque divergente',
  WITHDRAWAL_UNKNOWN: 'Saque com resultado desconhecido',
}

export const LEDGER_KIND_LABEL: Record<string, string> = {
  PAYMENT_RECEIVED: 'Pagamento recebido',
  REVENUE_RECOGNITION: 'Reconhecimento de receita',
  COMMISSION_CLOSE: 'Fechamento de comissão',
  COMMISSION_RELEASE: 'Liberação de comissão',
  COMMISSION_ADJUSTMENT: 'Ajuste de comissão',
  COMMISSION_CLAWBACK: 'Estorno de comissão',
  SELLER_COMMISSION: 'Comissão de vendedor',
  WITHDRAWAL_RESERVE: 'Reserva de saque',
  WITHDRAWAL_SETTLE: 'Saque pago',
  WITHDRAWAL_RELEASE: 'Saque devolvido ao saldo',
  EXPENSE: 'Despesa',
  PAYABLE_SETTLEMENT: 'Baixa de conta a pagar',
  REFUND: 'Reembolso',
  ADJUSTMENT: 'Ajuste manual',
  RECONCILIATION_ADJUSTMENT: 'Ajuste de conciliação',
  REVERSAL: 'Estorno de lançamento',
}

export const ACCOUNT_KIND_LABEL: Record<string, string> = {
  ASSET: 'Ativo',
  LIABILITY: 'Passivo',
  EQUITY: 'Patrimônio',
  INCOME: 'Receita',
  EXPENSE: 'Despesa',
  CONTRA_INCOME: 'Dedução de receita',
}

export const OWNER_TYPE_LABEL: Record<string, string> = {
  RD: 'RD',
  ADVERTISER: 'Anunciante',
  ESTABLISHMENT: 'Estabelecimento',
  SELLER: 'Vendedor',
}
