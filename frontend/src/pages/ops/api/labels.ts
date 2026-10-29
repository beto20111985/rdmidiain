/**
 * Rótulos pt-BR e mapeamentos de enums do domínio de Operação (FE-OPS).
 * Nunca exibir o código cru do enum ao usuário quando houver rótulo.
 */
import type {
  ContractKind,
  EquipmentType,
  LocationKind,
  MaintenanceStatus,
  MovementType,
  OnboardingStatus,
  PresenceStatus,
  Region,
  ScreenStatus,
} from './types'

export const UFS: Array<{ uf: string; name: string; region: Region }> = [
  { uf: 'AC', name: 'Acre', region: 'NORTE' },
  { uf: 'AL', name: 'Alagoas', region: 'NORDESTE' },
  { uf: 'AP', name: 'Amapá', region: 'NORTE' },
  { uf: 'AM', name: 'Amazonas', region: 'NORTE' },
  { uf: 'BA', name: 'Bahia', region: 'NORDESTE' },
  { uf: 'CE', name: 'Ceará', region: 'NORDESTE' },
  { uf: 'DF', name: 'Distrito Federal', region: 'CENTRO_OESTE' },
  { uf: 'ES', name: 'Espírito Santo', region: 'SUDESTE' },
  { uf: 'GO', name: 'Goiás', region: 'CENTRO_OESTE' },
  { uf: 'MA', name: 'Maranhão', region: 'NORDESTE' },
  { uf: 'MT', name: 'Mato Grosso', region: 'CENTRO_OESTE' },
  { uf: 'MS', name: 'Mato Grosso do Sul', region: 'CENTRO_OESTE' },
  { uf: 'MG', name: 'Minas Gerais', region: 'SUDESTE' },
  { uf: 'PA', name: 'Pará', region: 'NORTE' },
  { uf: 'PB', name: 'Paraíba', region: 'NORDESTE' },
  { uf: 'PR', name: 'Paraná', region: 'SUL' },
  { uf: 'PE', name: 'Pernambuco', region: 'NORDESTE' },
  { uf: 'PI', name: 'Piauí', region: 'NORDESTE' },
  { uf: 'RJ', name: 'Rio de Janeiro', region: 'SUDESTE' },
  { uf: 'RN', name: 'Rio Grande do Norte', region: 'NORDESTE' },
  { uf: 'RS', name: 'Rio Grande do Sul', region: 'SUL' },
  { uf: 'RO', name: 'Rondônia', region: 'NORTE' },
  { uf: 'RR', name: 'Roraima', region: 'NORTE' },
  { uf: 'SC', name: 'Santa Catarina', region: 'SUL' },
  { uf: 'SP', name: 'São Paulo', region: 'SUDESTE' },
  { uf: 'SE', name: 'Sergipe', region: 'NORDESTE' },
  { uf: 'TO', name: 'Tocantins', region: 'NORTE' },
]

export const REGION_LABEL: Record<Region, string> = {
  NORTE: 'Norte',
  NORDESTE: 'Nordeste',
  CENTRO_OESTE: 'Centro-Oeste',
  SUDESTE: 'Sudeste',
  SUL: 'Sul',
}

export const SCREEN_STATUS_LABEL: Record<ScreenStatus, string> = {
  ACTIVE: 'Ativa',
  MAINTENANCE: 'Em manutenção',
  INACTIVE: 'Inativa',
  DECOMMISSIONED: 'Retirada definitivamente',
}

export const ORIENTATION_LABEL: Record<string, string> = { HORIZONTAL: 'Horizontal', VERTICAL: 'Vertical' }
export const DISPLAY_TYPE_LABEL: Record<string, string> = {
  TV: 'TV',
  MONITOR: 'Monitor',
  LED_PANEL: 'Painel de LED',
  PROJECTOR: 'Projetor',
  OTHER: 'Outro',
}
export const PLAYER_TYPE_LABEL: Record<string, string> = {
  TVBOX: 'TV Box',
  SMART_TV_APP: 'App na Smart TV',
  PC: 'Computador',
  BROWSER: 'Navegador',
  OTHER: 'Outro',
}
export const TV_OWNER_LABEL: Record<string, string> = { RD: 'TV da RD (comodato)', ESTABLISHMENT: 'TV do estabelecimento' }
export const CLASS_LABEL: Record<string, string> = { STANDARD: 'Padrão', PREMIUM: 'Premium' }
export const PROGRAMMING_MODE_LABEL: Record<string, string> = {
  ESTABLISHMENT_DEFAULT: 'Grade comum do estabelecimento',
  CUSTOM: 'Grade própria desta tela',
}
export const AUDIENCE_METHOD_LABEL: Record<string, string> = {
  MANUAL: 'Informada manualmente',
  FLOW_ESTIMATE: 'Estimativa por fluxo de visitantes × visibilidade',
}
export const ONBOARDING_STATUS_EST_LABEL: Record<string, string> = {
  AWAITING_CONTRACT: 'Aguardando aceite do contrato',
  ACTIVE: 'Contrato aceito',
}

/** Presença (W-PLAY): palavra + tom + ícone (o ícone fica no componente). */
export const PRESENCE_LABEL: Record<PresenceStatus, string> = {
  conectado: 'Conectado',
  online: 'Online',
  offline: 'Offline',
  sincronizando: 'Sincronizando',
  sincronizado: 'Sincronizado',
  reproduzindo: 'Reproduzindo',
  travado: 'Travado',
  erro: 'Erro',
}

export const PRESENCE_TONE: Record<PresenceStatus, 'success' | 'info' | 'warning' | 'danger' | 'neutral'> = {
  conectado: 'info',
  online: 'success',
  offline: 'danger',
  sincronizando: 'warning',
  sincronizado: 'success',
  reproduzindo: 'success',
  travado: 'danger',
  erro: 'danger',
}

export function isPresenceStatus(v: unknown): v is PresenceStatus {
  return typeof v === 'string' && v in PRESENCE_LABEL
}

export const COMMAND_LABEL: Record<string, string> = {
  restart_app: 'Reiniciar o app',
  resync: 'Ressincronizar',
  clear_cache: 'Limpar cache',
  redownload: 'Baixar mídias novamente',
  update_app: 'Atualizar o app',
  reboot: 'Reiniciar o aparelho',
  reload_manifest: 'Recarregar programação',
  set_tech_pin: 'Definir PIN técnico',
}

export const COMMAND_HELP: Record<string, string> = {
  restart_app: 'Fecha e reabre o player. A exibição para por alguns segundos.',
  resync: 'Pede ao player que busque a programação mais recente agora.',
  clear_cache: 'Apaga as mídias baixadas. O player precisará baixar tudo de novo (consome internet).',
  redownload: 'Baixa novamente as mídias da programação atual.',
  update_app: 'Instala a versão liberada do app para o grupo deste dispositivo.',
  reboot: 'Reinicia o aparelho (TV Box/PC). A tela fica sem exibição até o sistema voltar.',
  reload_manifest: 'Recarrega a programação já baixada.',
}

/** Comandos que o backend exige confirmação explícita (commands.CONFIRM_REQUIRED). */
export const CONFIRM_REQUIRED_COMMANDS = new Set(['reboot', 'clear_cache', 'update_app'])

export const COMMAND_STATUS_LABEL: Record<string, string> = {
  PENDING: 'Pendente',
  DELIVERED: 'Entregue',
  RECEIVED: 'Recebido',
  DONE: 'Concluído',
  FAILED: 'Falhou',
  UNSUPPORTED: 'Não suportado',
  EXPIRED: 'Expirado',
  CANCELLED: 'Cancelado',
}

export const SCREEN_HISTORY_LABEL: Record<string, string> = {
  CREATED: 'Tela cadastrada',
  UPDATED: 'Dados alterados',
  STATUS_CHANGED: 'Status alterado',
  MOVED: 'Mudança de estabelecimento',
  HARDWARE_CHANGED: 'Hardware alterado',
  TV_REPLACED: 'Troca de TV',
  PLAYER_REPLACED: 'Troca de TV Box/player',
  SELLABLE_CHANGED: 'Disponibilidade para venda alterada',
  DECOMMISSIONED: 'Retirada definitiva',
  AUDIENCE_UPDATED: 'Audiência estimada atualizada',
}

export const CONTRACT_KIND_LABEL: Record<ContractKind, string> = {
  PARTNERSHIP: 'Contrato de parceria (estabelecimento)',
  ADVERTISER_TERMS: 'Termos do anunciante',
  COMODATO: 'Contrato de comodato',
}

export const CONTRACT_STATUS_LABEL: Record<string, string> = {
  DRAFT: 'Rascunho',
  PUBLISHED: 'Publicada',
  RETIRED: 'Substituída',
}

export const ACCEPTANCE_METHOD_LABEL: Record<string, string> = {
  ELECTRONIC: 'Aceite eletrônico',
  MANUAL_RECORD: 'Registro manual (contrato em papel)',
}

export const TV_PROVISION_LABEL: Record<string, string> = {
  OWN_TV: 'TV própria do estabelecimento',
  RD_TV_COMODATO: 'TV da RD em comodato',
}
export const UPGRADE_POLICY_LABEL: Record<string, string> = {
  FREE: 'Gratuito',
  PAID: 'Pago',
  NEGOTIATED: 'Negociado caso a caso',
}
export const PAID_BY_LABEL: Record<string, string> = { RD: 'RD Mídia Indoor', ESTABLISHMENT: 'Estabelecimento' }

export const ONBOARDING_STATUS_LABEL: Record<OnboardingStatus, string> = {
  INVITED: 'Convidado',
  IN_PROGRESS: 'Em preenchimento',
  SUBMITTED: 'Aguardando aprovação',
  APPROVED: 'Aprovado',
  REJECTED: 'Recusado',
  CANCELLED: 'Cancelado',
}
export const ONBOARDING_ORIGIN_LABEL: Record<string, string> = {
  ADMIN: 'Cadastro pela equipe RD',
  INVITE: 'Convite do comercial',
  SELF_SIGNUP: 'Auto-cadastro',
}
export const ONBOARDING_STEP_LABEL: Record<string, string> = {
  company: 'Empresa',
  establishment: 'Estabelecimento',
  address: 'Endereço',
  screens: 'Telas',
  commercial: 'Comercial',
  installation: 'Técnico e instalação',
}
export const DOCUMENT_KIND_LABEL: Record<string, string> = {
  COMPANY_REGISTRATION: 'Contrato social / cartão CNPJ',
  ID_DOCUMENT: 'Documento de identidade',
  PROOF_OF_ADDRESS: 'Comprovante de endereço',
  OTHER: 'Outro',
}

export const EQUIPMENT_TYPE_LABEL: Record<EquipmentType, string> = {
  TV: 'TV',
  TVBOX: 'TV Box',
  CABLE: 'Cabo',
  REMOTE: 'Controle remoto',
  MOUNT: 'Suporte',
  ACCESSORY: 'Acessório',
}
export const EQUIPMENT_STATUS_LABEL: Record<string, string> = {
  IN_STOCK: 'Em estoque',
  WITH_TECHNICIAN: 'Com técnico',
  INSTALLED: 'Instalado',
  LOANED: 'Emprestado',
  COMODATO: 'Em comodato',
  IN_MAINTENANCE: 'Em manutenção',
  IN_RMA: 'Em RMA (garantia)',
  WRITTEN_OFF: 'Baixado',
  BULK: 'Item de quantidade',
}
export const CONDITION_LABEL: Record<string, string> = { NEW: 'Novo', GOOD: 'Bom', DAMAGED: 'Avariado', BROKEN: 'Quebrado' }
export const LOCATION_KIND_LABEL: Record<LocationKind, string> = {
  EXTERNAL: 'Externo (fornecedor)',
  WAREHOUSE: 'Estoque',
  TECHNICIAN: 'Técnico',
  ESTABLISHMENT: 'Estabelecimento',
  SCREEN: 'Tela',
  MAINTENANCE: 'Manutenção',
  RMA: 'RMA (garantia)',
  DISPOSED: 'Baixa (descarte)',
}
export const MOVEMENT_TYPE_LABEL: Record<MovementType, string> = {
  ENTRY: 'Entrada',
  TRANSFER: 'Transferência',
  INSTALL: 'Instalação em tela',
  LOAN: 'Empréstimo',
  COMODATO: 'Comodato',
  RETURN: 'Devolução',
  MAINTENANCE_IN: 'Envio para manutenção',
  MAINTENANCE_OUT: 'Retorno da manutenção',
  RMA_OUT: 'Envio para RMA',
  RMA_RETURN: 'Retorno de RMA',
  WRITE_OFF: 'Baixa',
  ADJUSTMENT: 'Ajuste de inventário',
}

/** Matriz origem → destino do backend (assets.service.ALLOWED). */
export const MOVEMENT_RULES: Record<MovementType, { from: LocationKind[]; to: LocationKind[] }> = {
  ENTRY: { from: ['EXTERNAL'], to: ['WAREHOUSE'] },
  TRANSFER: { from: ['WAREHOUSE', 'TECHNICIAN'], to: ['WAREHOUSE', 'TECHNICIAN'] },
  INSTALL: { from: ['WAREHOUSE', 'TECHNICIAN'], to: ['SCREEN'] },
  LOAN: { from: ['WAREHOUSE', 'TECHNICIAN'], to: ['ESTABLISHMENT'] },
  COMODATO: { from: ['WAREHOUSE', 'TECHNICIAN'], to: ['ESTABLISHMENT', 'SCREEN'] },
  RETURN: { from: ['ESTABLISHMENT', 'SCREEN', 'TECHNICIAN'], to: ['WAREHOUSE'] },
  MAINTENANCE_IN: { from: ['WAREHOUSE', 'TECHNICIAN', 'ESTABLISHMENT', 'SCREEN'], to: ['MAINTENANCE'] },
  MAINTENANCE_OUT: { from: ['MAINTENANCE'], to: ['WAREHOUSE', 'SCREEN'] },
  RMA_OUT: { from: ['WAREHOUSE', 'MAINTENANCE'], to: ['RMA'] },
  RMA_RETURN: { from: ['RMA'], to: ['WAREHOUSE'] },
  WRITE_OFF: { from: ['WAREHOUSE', 'TECHNICIAN', 'ESTABLISHMENT', 'SCREEN', 'MAINTENANCE', 'RMA'], to: ['DISPOSED'] },
  ADJUSTMENT: { from: ['EXTERNAL', 'WAREHOUSE'], to: ['WAREHOUSE', 'DISPOSED'] },
}

export const MAINTENANCE_STATUS_LABEL: Record<MaintenanceStatus, string> = {
  OPEN: 'Aberta',
  IN_PROGRESS: 'Em andamento',
  WAITING_PARTS: 'Aguardando peças',
  RESOLVED: 'Resolvida',
  CANCELLED: 'Cancelada',
}
/** Transições permitidas (installations.service.TRANSITIONS). */
export const MAINTENANCE_TRANSITIONS: Record<MaintenanceStatus, MaintenanceStatus[]> = {
  OPEN: ['IN_PROGRESS', 'WAITING_PARTS', 'RESOLVED', 'CANCELLED'],
  IN_PROGRESS: ['WAITING_PARTS', 'RESOLVED', 'CANCELLED'],
  WAITING_PARTS: ['IN_PROGRESS', 'RESOLVED', 'CANCELLED'],
  RESOLVED: [],
  CANCELLED: [],
}
export const PRIORITY_LABEL: Record<string, string> = { LOW: 'Baixa', NORMAL: 'Normal', HIGH: 'Alta', URGENT: 'Urgente' }
export const COMPONENT_LABEL: Record<string, string> = {
  TV: 'TV',
  TVBOX: 'TV Box',
  CABLE: 'Cabos',
  NETWORK: 'Rede/internet',
  OTHER: 'Outro',
}
export const MAINTENANCE_EVENT_LABEL: Record<string, string> = {
  OPENED: 'Ordem aberta',
  ASSIGNED: 'Técnico designado',
  STATUS_CHANGED: 'Status alterado',
  IN_PROGRESS: 'Em andamento',
  WAITING_PARTS: 'Aguardando peças',
  RESOLVED: 'Resolvida',
  CANCELLED: 'Cancelada',
  NOTE: 'Anotação',
}

export const INSTALLATION_STATUS_LABEL: Record<string, string> = {
  SCHEDULED: 'Agendada',
  IN_PROGRESS: 'Em andamento',
  COMPLETED: 'Concluída',
  CANCELLED: 'Cancelada',
}
export const PHOTO_KIND_LABEL: Record<string, string> = {
  TV_WORKING: 'TV funcionando',
  EQUIPMENT: 'Equipamentos',
  SITE: 'Local da instalação',
  OTHER: 'Outra',
}
export const CONNECTION_TEST_LABEL: Record<string, string> = {
  ONLINE: 'Player conectado (presença recente)',
  OFFLINE: 'Player sem comunicação',
  NOT_PAIRED: 'Nenhum dispositivo pareado a esta tela',
  UNAVAILABLE: 'Presença indisponível no momento',
}
export const INSTALLATION_MISSING_LABEL: Record<string, string> = {
  'photo:TV_WORKING': 'Foto da TV funcionando',
  signature: 'Assinatura do responsável',
  geolocation: 'Localização (GPS) do técnico',
  screen: 'Tela vinculada à instalação',
  connection_test: 'Teste de conexão ONLINE recente (ou dispensa justificada com 20+ caracteres)',
}

export const FALLBACK_PURPOSE_LABEL: Record<string, string> = {
  ESTABLISHMENT_LOGO: 'Logo do estabelecimento',
  RD_DEFAULT: 'Padrão RD',
  RD_PROMO: 'Divulgação RD',
  INSTITUTIONAL_MESSAGE: 'Mensagem institucional',
}
export const EMERGENCY_TARGET_LABEL: Record<string, string> = {
  ALL: 'Todas as telas',
  SCREENS: 'Telas selecionadas',
  ESTABLISHMENTS: 'Estabelecimentos selecionados',
  STATES: 'Estados (UF)',
  CITIES: 'Cidades',
}
export const CREATIVE_KIND_LABEL: Record<string, string> = {
  EXTERNAL: 'Anúncio',
  INTERNAL: 'Conteúdo interno',
  INSTITUTIONAL: 'Institucional RD',
  FALLBACK: 'Contingência (fallback)',
}

export const CREATIVE_STATUS_LABEL: Record<string, string> = {
  RASCUNHO: 'Aguardando revisão',
  EM_ANALISE: 'Em análise',
  APROVADO: 'Aprovada',
  REJEITADO: 'Rejeitada',
  SUBSTITUIDO: 'Substituída',
  ARQUIVADO: 'Arquivada',
}

export const CREATIVE_STATUS_TONE: Record<string, 'neutral' | 'warning' | 'success' | 'danger'> = {
  RASCUNHO: 'warning',
  EM_ANALISE: 'warning',
  APROVADO: 'success',
  REJEITADO: 'danger',
  SUBSTITUIDO: 'neutral',
  ARQUIVADO: 'neutral',
}

export const TECHNICAL_STATUS_LABEL: Record<string, string> = {
  PENDENTE: 'Mídia em processamento',
  VALIDO: 'Mídia válida',
  INVALIDO: 'Mídia inválida',
}

export function labelOf(map: Record<string, string>, value: string | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  return map[value] ?? value
}
