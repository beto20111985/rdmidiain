/**
 * SOMENTE DESENVOLVIMENTO — perfis fictícios para revisão visual do RBAC e do layout sem
 * backend. Tudo aqui é DEMONSTRAÇÃO (nomes contêm "Demonstração") e não existe no build de
 * produção (importado apenas por rotas `import.meta.env.DEV`).
 */
import { PERMISSIONS, type Permission } from '@/lib/permissions'
import type { ApprovalModeCode, Me, SecondMasterStatus, SetupState } from '@/features/auth/types'

const ESTABLISHMENT_OWNER: Permission[] = [
  'dashboard.view',
  'establishments.view',
  'screens.view',
  'playlists.reorder',
  'commissions.view',
  'withdrawals.view',
  'withdrawals.request',
  'reports.view',
  'support.create',
]
const ADVERTISER: Permission[] = [
  'campaigns.view',
  'campaigns.manage',
  'creatives.view',
  'creatives.manage',
  'checkout.create',
  'payments.view',
  'design.request',
  'design.approve',
  'reports.view',
  'support.create',
]
const FINANCIAL: Permission[] = [
  'finance.view',
  'finance.manage',
  'finance.reconcile',
  'payments.view',
  'commissions.view',
  'commissions.adjust',
  'withdrawals.view',
  'reports.view',
  'reports.financial',
  'dashboard.view',
]

/** Estado de primeiro acesso COERENTE com o perfil de demonstração (sem passo pendente; 2º Master só na dupla para Master). */
function setupFor(master: { mode: ApprovalModeCode; second: SecondMasterStatus } | null): SetupState {
  return {
    required: false,
    next_step: master && master.mode === 'DUAL' && master.second !== 'DONE' ? 'SECOND_MASTER' : null,
    steps: {
      password_change: 'DONE',
      totp: 'DONE',
      financial_password: master ? 'DONE' : 'NOT_REQUIRED',
      second_master: master && master.mode === 'DUAL' ? master.second : 'NOT_REQUIRED',
    },
    is_admin_master: !!master,
    approval_mode: master ? master.mode : null,
    dual_approval_ready: master && master.mode === 'DUAL' ? master.second === 'DONE' : null,
  }
}

function base(name: string, permissions: readonly string[], contexts: Me['contexts'], extra: Partial<Me> = {}): Me {
  return {
    user: {
      id: '00000000-0000-0000-0000-00000000demo',
      name,
      email: 'demonstracao@exemplo.invalid',
      phone: null,
      mfa_enabled: true,
      is_admin_master: false,
      theme: null,
    },
    permissions: [...permissions],
    assignments: [],
    contexts,
    impersonation: { active: false, real_user: null },
    mfa: { required: false, configured: true },
    security_setup: setupFor(null),
    ...extra,
  }
}

function master(mode: ApprovalModeCode, second: SecondMasterStatus): Me {
  const me = base(`Admin Master Demonstração (${mode === 'SINGLE' ? 'única' : 'dupla'})`, PERMISSIONS, [RD], { security_setup: setupFor({ mode, second }) })
  return { ...me, user: { ...me.user, is_admin_master: true } }
}

const RD = { key: 'rd', label: 'RD Mídia Indoor', kind: 'PLATFORM' as const }
const EST = { key: 'est:demo', label: 'Minha unidade — Estabelecimento Demonstração', kind: 'ESTABLISHMENT' as const, org_id: 'demo' }
const ADV = { key: 'adv:demo', label: 'Minhas campanhas — Estabelecimento Demonstração', kind: 'ADVERTISER' as const, org_id: 'demo' }

export const DEMO_PROFILES: Array<{ id: string; label: string; description: string; me: Me }> = [
  {
    id: 'super',
    label: 'Super Admin (todas as permissões)',
    description: 'Contexto RD com o catálogo completo.',
    me: base('Super Admin Demonstração', PERMISSIONS, [RD]),
  },
  {
    id: 'master-dual',
    label: 'Admin Master — dupla aprovação (2º Master não convidado)',
    description: 'Mostra o modal e o banner do 2º Master (somente na dupla).',
    me: master('DUAL', 'NOT_INVITED'),
  },
  {
    id: 'master-dual-pendente',
    label: 'Admin Master — dupla aprovação (convite pendente)',
    description: 'Banner "Convite enviado… aguardando ativação".',
    me: master('DUAL', 'INVITE_PENDING'),
  },
  {
    id: 'primeiro-acesso',
    label: 'Master no primeiro acesso (dupla, tudo pendente)',
    description: 'Abre o assistente /primeiro-acesso (somente visual; nada é enviado).',
    me: (() => {
      const me = master('DUAL', 'NOT_INVITED')
      return {
        ...me,
        security_setup: {
          required: true,
          next_step: 'PASSWORD_CHANGE' as const,
          steps: { password_change: 'PENDING' as const, totp: 'PENDING' as const, financial_password: 'PENDING' as const, second_master: 'NOT_INVITED' as const },
          is_admin_master: true,
          approval_mode: 'DUAL' as const,
          dual_approval_ready: false,
        },
      }
    })(),
  },
  {
    id: 'master-single',
    label: 'Admin Master — aprovação única',
    description: 'Nenhum modal, banner ou menção a segundo Master.',
    me: master('SINGLE', 'NOT_REQUIRED'),
  },
  {
    id: 'financeiro',
    label: 'Financeiro',
    description: 'Somente módulos financeiros e relatórios.',
    me: base('Financeiro Demonstração', FINANCIAL, [RD]),
  },
  {
    id: 'estabelecimento',
    label: 'Dono de estabelecimento',
    description: 'Contexto "Minha unidade" (sem saque na visão geral).',
    me: base('Dono Demonstração', ESTABLISHMENT_OWNER, [EST]),
  },
  {
    id: 'anunciante',
    label: 'Anunciante',
    description: 'Contexto "Minhas campanhas".',
    me: base('Anunciante Demonstração', ADVERTISER, [ADV]),
  },
  {
    id: 'misto',
    label: 'Estabelecimento que também anuncia',
    description: 'Mesma conta alterna entre "Minha unidade" e "Minhas campanhas" (E2 #779–784).',
    me: base('Conta mista Demonstração', [...new Set([...ESTABLISHMENT_OWNER, ...ADVERTISER])], [EST, ADV]),
  },
  {
    id: 'impersonation',
    label: 'Admin acessando como cliente',
    description: 'Exibe o banner fixo de impersonation (P001 §96).',
    me: base('Anunciante Demonstração', ADVERTISER, [ADV], {
      impersonation: { active: true, real_user: { id: 'demo-admin', name: 'Admin Demonstração' } },
    }),
  },
]
