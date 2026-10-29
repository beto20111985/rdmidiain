import { defaultMessageFor, errorMessage, isApiError } from '@/lib/errors'
import { formatDateTime, formatTime } from '@/lib/format'

/**
 * Mensagens dos erros de autenticação (contrato v1 §2). O 401 do PRÓPRIO login chega
 * aqui intacto (o cliente HTTP não tenta refresh nem redireciona nas rotas de auth).
 */
export function loginErrorMessage(e: unknown, now: Date = new Date()): string {
  if (!isApiError(e)) return 'Não foi possível entrar. Tente novamente.'
  if (e.status === 401) {
    return e.code !== 'HTTP_401' && e.message ? e.message : 'E-mail, telefone ou senha incorretos.'
  }
  if (e.status === 423) {
    const s = e.retryAfterSeconds
    if (s && s > 0) {
      const until = new Date(now.getTime() + s * 1000)
      const minutes = Math.ceil(s / 60)
      return `Conta bloqueada temporariamente por excesso de tentativas. Tente novamente em ${minutes} ${minutes === 1 ? 'minuto' : 'minutos'} (às ${formatTime(until)}).`
    }
    return 'Conta bloqueada temporariamente por excesso de tentativas. Tente novamente mais tarde.'
  }
  if (e.status === 429) {
    const s = e.retryAfterSeconds
    return s && s > 0
      ? `Muitas tentativas em pouco tempo. Aguarde ${s} ${s === 1 ? 'segundo' : 'segundos'} e tente novamente.`
      : 'Muitas tentativas em pouco tempo. Aguarde um pouco e tente novamente.'
  }
  return e.message
}

export function mfaErrorMessage(e: unknown): string {
  if (isApiError(e) && e.status === 401) return 'Código inválido ou já utilizado. Confira o aplicativo autenticador e tente de novo.'
  return loginErrorMessage(e)
}

/** Aceita apenas caminhos internos para o redirecionamento pós-login. */
export function safeNext(next: string | null | undefined): string {
  if (!next || !next.startsWith('/') || next.startsWith('//') || next.startsWith('/login')) return '/'
  return next
}

// ---------------------------------------------------------------------------
// AÇÃO 003 — códigos novos (SPEC §6). A mensagem do servidor é a fonte; esta tabela é o reserva
// (envelope sem mensagem) e a referência única dos textos em pt-BR.
// ---------------------------------------------------------------------------
export const SECURITY_ERROR_MESSAGES: Record<string, string> = {
  FINANCIAL_PASSWORD_SETUP_REQUIRED: 'Crie sua senha financeira para continuar.',
  SETUP_STEP_OUT_OF_ORDER: 'Conclua a etapa anterior da configuração de segurança.',
  PASSWORD_CHANGE_NOT_REQUIRED: 'Troca de senha não pendente. Use Minha conta → Segurança.',
  ROLE_RESERVED_FOR_ADMIN_MASTER: 'O perfil Super Admin é exclusivo dos Administradores Master.',
  MASTER_INVITE_ALREADY_PENDING: 'Já existe um convite pendente para o segundo Administrador Master.',
  ADMIN_MASTER_SEATS_FULL: 'Os dois assentos de Administrador Master já estão ocupados.',
  APPROVAL_MODE_SINGLE: 'Esta instalação usa aprovação financeira única.',
  APPROVAL_MODE_DUAL: 'Esta instalação usa dupla aprovação: outro Administrador Master precisa aprovar.',
  APPROVAL_MODE_CHANGED: 'O modo de aprovação mudou; crie uma nova solicitação.',
  APPROVAL_MODE_CHANGE_INVALID: 'Mudança de modo inválida.',
  ONLY_REQUESTER_AUTHORIZES: 'Somente quem solicitou pode autorizar no modo de aprovação única.',
  SECOND_MASTER_REQUIRED:
    'Esta instalação usa dupla aprovação financeira. Cadastre e ative o segundo Administrador Master para liberar esta operação.',
  EMAIL_ALREADY_REGISTERED: 'Este e-mail já pertence a outra conta.',
  MASTER_INVITE_INVALID: 'Convite inválido ou expirado.',
  MASTER_INVITE_LOCKED: 'Convite bloqueado por tentativas inválidas. Peça um novo convite.',
  MASTER_INVITE_RATE_LIMITED: 'Muitos reenvios. Tente mais tarde.',
  MASTER_RECENTLY_RECOVERED: 'Credenciais recuperadas recentemente: operações financeiras bloqueadas temporariamente.',
  INSTANCE_NOT_INITIALIZED: 'Instância não inicializada. Contate o responsável técnico.',
  SECURITY_INTEGRITY_ALERT:
    'Verificação de integridade da segurança falhou. Operações financeiras suspensas. Contate o responsável técnico.',
  ADMIN_MASTER_REQUIRED: 'Esta ação é exclusiva dos Administradores Master.',
  // Backend financeiro (agente C): recusas do banco mapeadas para 409
  APPROVER_NOT_ACTIVE_MASTER: 'Somente um Administrador Master ativo pode aprovar.',
  SELF_AUTHORIZER_INVALID: 'Somente o solicitante (Administrador Master ativo) pode autorizar.',
  REQUESTER_NOT_ACTIVE_MASTER: 'Somente um Administrador Master ativo pode solicitar.',
  MASTER_INVITE_INVALID_STATE: 'Estado inválido para o convite do segundo Administrador Master.',
}

/** Mensagem do servidor; se o envelope veio sem mensagem própria, o texto da tabela da SPEC §6. */
export function securityErrorMessage(e: unknown): string {
  if (!isApiError(e)) return errorMessage(e)
  const fallback = SECURITY_ERROR_MESSAGES[e.code]
  const base = fallback && (!e.message || e.message === defaultMessageFor(e.status)) ? fallback : e.message
  // 423 MASTER_RECENTLY_RECOVERED: informa até quando as operações financeiras ficam bloqueadas.
  const until = e.code === 'MASTER_RECENTLY_RECOVERED' ? e.details.blocked_until : null
  if (typeof until === 'string' && !Number.isNaN(new Date(until).getTime())) return `${base} Liberado em ${formatDateTime(until)}.`
  return base
}

/** Erros dos passos do primeiro acesso (senha, TOTP, senha financeira). */
export function setupErrorMessage(e: unknown): string {
  if (!isApiError(e)) return errorMessage(e)
  switch (e.code) {
    case 'INVALID_PASSWORD':
      return e.message && e.message !== defaultMessageFor(e.status) ? e.message : 'Senha atual incorreta.'
    case 'PASSWORD_POLICY':
      return e.message || 'A nova senha não atende à política de segurança.'
    case 'PASSWORD_REUSED':
      return e.message || 'Escolha uma senha que você ainda não usou.'
    case 'FINANCIAL_STEP_UP_INVALID':
      return e.message || 'Senha de login ou código do autenticador incorretos.'
    case 'FINANCIAL_STEP_UP_LOCKED':
      return e.message || 'Muitas tentativas. Aguarde antes de tentar novamente.'
    case 'FINANCIAL_PASSWORD_POLICY':
      return e.message || 'A senha financeira não atende à política de segurança.'
    default:
      break
  }
  if (e.status === 423 || e.status === 429) return loginErrorMessage(e)
  return securityErrorMessage(e)
}

/**
 * Ativação do 2º Master (pública). Mensagens genéricas: não revelam se o convite não existe,
 * expirou, foi revogado ou aceito (SPEC §5.1).
 */
export function masterActivationErrorMessage(e: unknown): string {
  if (!isApiError(e)) return errorMessage(e)
  switch (e.code) {
    case 'MASTER_INVITE_INVALID':
      return SECURITY_ERROR_MESSAGES.MASTER_INVITE_INVALID
    case 'MASTER_INVITE_LOCKED':
      return SECURITY_ERROR_MESSAGES.MASTER_INVITE_LOCKED
    case 'INVALID_MFA_CODE':
      return 'Código do autenticador inválido. Confira o horário do celular e digite o código atual.'
    case 'MFA_SETUP_NOT_STARTED':
      return 'O autenticador ainda não foi cadastrado. Gere a chave e cadastre-a no aplicativo antes de concluir.'
    case 'PASSWORD_POLICY':
    case 'FINANCIAL_PASSWORD_POLICY':
      return e.message || 'A senha não atende à política de segurança.'
    default:
      break
  }
  if (e.status === 429) {
    const s = e.retryAfterSeconds
    return s && s > 0 ? `Muitas tentativas em pouco tempo. Aguarde ${s} ${s === 1 ? 'segundo' : 'segundos'} e tente novamente.` : 'Muitas tentativas em pouco tempo. Aguarde um pouco e tente novamente.'
  }
  return securityErrorMessage(e)
}
