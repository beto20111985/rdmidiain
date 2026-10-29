/** Seletor de CLIENTE do CRM (W-BIZ `GET /crm/client-options`): anunciantes/agências ativos, sem a instância. */
import type { PickerOption } from '@/components/campaigns/entity-picker'
import { crmApi, type ClientOption } from './api'

export const INSTANCE_CLIENT_MESSAGE = 'A organização da própria RD não pode ser cliente de proposta. Escolha um anunciante ou uma agência.'

/** Opção do seletor de cliente: nome fantasia (ou razão social) + razão social, cidade/UF e papel. */
export function clientOptionToPicker(o: ClientOption): PickerOption {
  const label = o.trade_name || o.name
  const description = [
    o.trade_name && o.trade_name !== o.name ? o.name : null,
    o.city ? `${o.city}${o.uf ? `/${o.uf}` : ''}` : null,
    o.business_roles.includes('AGENCY') ? 'Agência' : null,
  ]
    .filter(Boolean)
    .join(' · ')
  return { id: o.id, label, description: description || undefined }
}

export const searchClients = async (text: string) => (await crmApi.clientOptions(text, 20)).items.map(clientOptionToPicker)
