/** Tipos das respostas de `/notifications` (W-BIZ). */
export interface NotificationItem {
  id: string
  event_type: string
  title: string
  body: string
  data: Record<string, unknown>
  link: string | null
  entity_type: string | null
  entity_id: string | null
  read: boolean
  read_at: string | null
  created_at: string
  deliveries: Array<{ channel: string; status: string; detail: string | null }>
}

export interface PreferenceChannel {
  channel: 'IN_APP' | 'EMAIL' | 'PUSH'
  enabled: boolean
  locked: boolean
  default_channel: boolean
  available: boolean
  unavailable_reason: string | null
}

export interface PreferenceItem {
  event_type: string
  label: string
  critical: boolean
  channels: PreferenceChannel[]
}

export interface TemplateItem {
  event_type: string
  channel: 'IN_APP' | 'EMAIL' | 'PUSH'
  custom: boolean
  is_active: boolean
  title_template: string
  body_template: string
  version: number
  variables: string[]
}
