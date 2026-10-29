/** Tipos das respostas da API usadas pelas páginas de Administração (OpenAPI atual do backend). */
import type { ScopeType } from '@/components/admin/labels'

export interface Organization {
  id: string
  name: string
  trade_name: string | null
  document: string
  document_formatted: string
  document_type: 'CPF' | 'CNPJ'
  person_type: 'PF' | 'PJ'
  business_roles: string[]
  business_role_labels: string[]
  is_active: boolean
  kyc_status: 'PENDING' | 'APPROVED' | 'REJECTED' | string
  city: string | null
  state: string | null
  city_key: string | null
  responsible_name?: string | null
  email?: string | null
  phone?: string | null
  address?: string | null
  number?: string | null
  complement?: string | null
  neighborhood?: string | null
  postal_code?: string | null
  deactivated_at?: string | null
  deactivation_reason?: string | null
  kyc_reviewed_at?: string | null
  kyc_notes?: string | null
  notes?: string | null
  created_at?: string | null
  updated_at?: string | null
}

export interface AgencyLink {
  id: string
  agency_org_id: string
  client_org_id: string
  client_name: string | null
  status: 'ACTIVE' | 'ENDED' | string
  created_at: string | null
  ended_at: string | null
  end_reason: string | null
}

export interface ApprovalPolicies {
  global_mode: 'MANUAL' | 'AUTO' | string
  items: Array<{ org_id: string; mode: 'MANUAL' | 'AUTO'; reason: string | null; updated_by: string | null; updated_at: string | null }>
}

export interface UserAssignment {
  id: string
  role: string
  scope_type: ScopeType
  scope_id: string | null
  scope_value: string | null
}

export interface UserRow {
  id: string
  name: string
  is_active: boolean
  is_admin_master: boolean
  restricted: boolean
  email?: string
  phone?: string | null
  mfa_enabled?: boolean
  must_change_password?: boolean
  last_login?: string | null
  created_at?: string | null
  locked?: boolean
  assignments?: UserAssignment[]
}

export interface Invite {
  id: string
  email: string
  role: string
  scope_type: ScopeType
  scope_id: string | null
  scope_value: string | null
  expires_at: string
  created_at?: string
}

export interface PermissionInfo {
  code: string
  name: string
  module: string
}

export interface AuditRow {
  id: string
  seq: number
  created_at: string
  actor_user_id: string | null
  real_user_id: string | null
  actor_device_id: string | null
  org_id: string | null
  action: string
  entity_type: string | null
  entity_id: string | null
  before: unknown
  after: unknown
  reason: string | null
  ip: string | null
  user_agent: string | null
  origin: string | null
  request_id: string | null
  row_hash: string | null
}

export interface AuditVerify {
  ok: boolean
  rows_checked: number
  first_bad_seq: number | null
  problem: string | null
}

export interface SettingItem {
  key: string
  value: unknown
  default: unknown
  type: 'int' | 'str' | 'bool' | 'enum' | 'list[int]' | string
  choices: string[] | null
  min: number | null
  max: number | null
  financial: boolean
  category: string
  description: string
  version: number
  updated_at: string | null
  updated_by: string | null
}
