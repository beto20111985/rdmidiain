/** Tipos das respostas de `/support` (W-BIZ). Campos comerciais só vêm para a equipe RD. */
export interface Ticket {
  id: string
  number: string
  org_id: string | null
  establishment_id: string | null
  screen_id: string | null
  creative_code: string | null
  category: string
  title: string
  description: string
  status: string
  priority: string
  created_by: string
  assigned: boolean
  sla_due_at: string | null
  sla_breached: boolean
  first_response_at: string | null
  resolved_at: string | null
  closed_at: string | null
  created_at: string
  updated_at: string | null
  // Somente equipe RD (o servidor omite para clientes/estabelecimentos):
  assigned_to?: string | null
  creative_id?: string | null
  campaign_id?: string | null
  advertiser_org_id?: string | null
  city_key?: string | null
  creative_found?: boolean | null
}

export interface TicketMessage {
  id: string
  /** null quando o autor é da equipe RD e quem vê é o cliente (o servidor oculta o id). */
  author_id: string | null
  body: string
  is_internal: boolean
  created_at: string
  /** Nome exibível conforme a regra do servidor (cliente vê "Equipe RD"). */
  author_name?: string | null
  author_is_staff?: boolean | null
}

export interface TicketDetail extends Ticket {
  history: Array<{ type: string; from: string | null; to: string | null; at: string }>
  messages: TicketMessage[]
}

export interface CreativeLookup {
  creative: { id: string; code: string; name: string; status: string; kind: string | null; duration_seconds: number | null }
  advertiser: { org_id: string; name: string | null }
  campaigns: Array<{ id: string; name: string; status: string; start_date: string | null; end_date: string | null }>
  tickets: Array<{ id: string; number: string; status: string; category: string; created_at: string }>
}

/** A resposta traz os campos da equipe somente quando o servidor considera o ator "staff". */
export function isStaffView(t: Ticket): boolean {
  return Object.prototype.hasOwnProperty.call(t, 'assigned_to')
}
