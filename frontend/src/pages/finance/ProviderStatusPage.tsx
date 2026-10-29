/** Financeiro → Status do provedor (CloudPayPag): modo, credenciais configuradas e capacidades. */
import { PageHeader } from '@/components/layout/page-meta'
import { ProviderStatusCard } from './provider-status'

export default function ProviderStatusPage() {
  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Status do provedor"
        description="Integração CloudPayPag desta instância"
        breadcrumbs={[{ label: 'Financeiro', to: '/financeiro' }, { label: 'Status do provedor' }]}
      />
      <ProviderStatusCard />
    </div>
  )
}
