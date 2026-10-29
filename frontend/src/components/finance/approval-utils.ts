/**
 * Utilidades dos pedidos de aprovação financeira criados a partir das páginas financeiras.
 * Desde a AÇÃO 003 a instância tem um MODO de aprovação: ÚNICA (o próprio Master autoriza com
 * TOTP + senha financeira + código por e-mail) ou DUPLA (um solicita, o outro Master aprova).
 * Os textos mudam conforme o modo; na ÚNICA não há nenhuma menção a outro/segundo Master.
 */
import { toast } from 'sonner'
import { useCan } from '@/hooks/usePermissions'
import { useAuthStore } from '@/stores/authStore'
import { formatCents } from '@/lib/format'
import { useApprovalModeInfo } from '@/features/security/hooks'
import type { ApprovalModeCode } from '@/features/auth/types'

/** Só os Admin Master (titulares de assento) solicitam/aprovam/autorizam (finance.approve efetivo apenas para eles). */
export function useIsAdminMaster(): boolean {
  const can = useCan()
  const master = useAuthStore((s) => !!s.me?.user.is_admin_master)
  return master && can('finance.approve')
}

export interface InstanceApprovalMode {
  /** null = ainda desconhecido (carregando) ou sem permissão para ver. */
  mode: ApprovalModeCode | null
  isSingle: boolean
  isDual: boolean
  loading: boolean
}

/**
 * Modo de aprovação da instância. Titulares de assento recebem o modo em `/me.security_setup`;
 * os demais (financeiro, etc.) consultam `GET /security/approval-mode` (settings.view | finance.view).
 */
export function useInstanceApprovalMode(): InstanceApprovalMode {
  const can = useCan()
  const fromMe = useAuthStore((s) => s.me?.security_setup?.approval_mode ?? null)
  const canSee = can('settings.view') || can('finance.view')
  const query = useApprovalModeInfo(fromMe === null && canSee)
  const mode = fromMe ?? query.data?.mode ?? null
  return { mode, isSingle: mode === 'SINGLE', isDual: mode === 'DUAL', loading: fromMe === null && canSee && query.isLoading }
}

export const APPROVAL_MODE_LABEL: Record<ApprovalModeCode, string> = {
  SINGLE: 'Única',
  DUAL: 'Dupla',
}

/** Rótulo curto de um pedido: "Aprovação única" / "Dupla aprovação". */
export function approvalModeNoun(mode: ApprovalModeCode | null | undefined): string {
  if (mode === 'SINGLE') return 'Aprovação única'
  if (mode === 'DUAL') return 'Dupla aprovação'
  return 'Aprovação financeira'
}

/** O pedido ainda precisa ser autorizado pelo próprio solicitante (modo ÚNICA). */
export function needsSelfAuthorization(r: { approval_mode?: ApprovalModeCode; status?: string; can_authorize?: boolean } | null | undefined): boolean {
  return !!r && r.approval_mode === 'SINGLE' && r.status === 'PENDING' && r.can_authorize !== false
}

export interface ApprovalCopy {
  /** Frase para "o lançamento só acontece depois que …". */
  afterGate: string
  /** Rótulo do campo de motivo nos formulários. */
  reasonLabel: string
  /** Texto curto para o botão de envio. */
  submitLabel: string
}

/** Textos dos formulários que criam pedidos, conforme o modo (null = ainda desconhecido: neutro). */
export function approvalCopy(mode: ApprovalModeCode | null): ApprovalCopy {
  if (mode === 'SINGLE') {
    return {
      afterGate: 'você autorizar o pedido (TOTP, senha financeira e código por e-mail)',
      reasonLabel: 'Motivo (fica registrado na auditoria)',
      submitLabel: 'Criar pedido e autorizar',
    }
  }
  if (mode === 'DUAL') {
    return {
      afterGate: 'o outro Admin Master aprovar o pedido',
      reasonLabel: 'Motivo (visto pelo aprovador)',
      submitLabel: 'Solicitar aprovação',
    }
  }
  return {
    afterGate: 'o pedido ser autorizado conforme o modo de aprovação da instância',
    reasonLabel: 'Motivo (fica registrado e é visto na aprovação)',
    submitLabel: 'Criar pedido',
  }
}

export function toastApprovalCreated(r: { action_label?: string; amount_cents?: number | null; approval_mode?: ApprovalModeCode }) {
  const what = r.action_label ? `${r.action_label.charAt(0).toUpperCase()}${r.action_label.slice(1)}` : 'Operação'
  const amount = typeof r.amount_cents === 'number' ? ` (${formatCents(r.amount_cents)})` : ''
  if (r.approval_mode === 'SINGLE') {
    toast.success(`${what}${amount}: pedido criado. Falta a sua autorização em Financeiro → Aprovações.`)
    return
  }
  toast.success(`${what}${amount}: pedido criado. Aguardando a aprovação do outro Admin Master.`)
}
