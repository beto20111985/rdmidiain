/** Mensagens para os erros de gestão de usuários (convites, atribuições, impersonation). */
import { errorMessage, isApiError } from '@/lib/errors'

export interface ExplainedError {
  title: string
  detail: string
  code?: string
}

export function explainUserError(e: unknown): ExplainedError {
  if (!isApiError(e)) return { title: 'Não foi possível concluir', detail: errorMessage(e) }
  switch (e.code) {
    case 'ROLE_ESCALATION':
      return {
        title: 'Perfil acima do seu acesso',
        detail: `${e.message} Para conceder um perfil você precisa ter todas as permissões dele no mesmo escopo.`,
        code: e.code,
      }
    case 'ROLE_RESERVED_FOR_ADMIN_MASTER':
      return {
        title: 'Perfil reservado',
        detail: e.message || 'O perfil Super Admin é exclusivo dos Administradores Master.',
        code: e.code,
      }
    case 'ADMIN_MASTER_PROTECTED':
      return {
        title: 'Admin Master protegido',
        detail: `${e.message} Os dois Admin Master são independentes: um não altera o acesso do outro.`,
        code: e.code,
      }
    case 'IMPERSONATION_FORBIDDEN_TARGET':
      return { title: 'Acesso como este usuário não permitido', detail: e.message, code: e.code }
    case 'SELF_ASSIGNMENT':
    case 'SELF_MANAGEMENT':
      return { title: 'Ação sobre a própria conta', detail: e.message, code: e.code }
    default:
      if (e.status === 404)
        return {
          title: 'Fora do seu escopo',
          detail: 'O escopo escolhido não existe ou está fora do seu acesso. Escolha uma empresa, estabelecimento ou cidade que você administra.',
          code: e.code,
        }
      return { title: 'Não foi possível concluir', detail: e.message, code: e.code }
  }
}
