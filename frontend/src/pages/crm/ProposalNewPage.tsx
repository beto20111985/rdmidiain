/** CRM → Nova proposta (rascunho). `?lead=<id>` pré-seleciona o lead. */
import { useNavigate, useSearchParams } from 'react-router-dom'
import { PageHeader } from '@/components/layout/page-meta'
import { Card } from '@/components/ui/card'
import { ProposalForm } from './proposal-form'

export default function ProposalNewPage() {
  const [sp] = useSearchParams()
  const navigate = useNavigate()
  return (
    <div className="flex flex-col gap-6">
      <PageHeader title="Nova proposta" breadcrumbs={[{ label: 'Propostas', to: '/crm/propostas' }, { label: 'Nova proposta' }]} />
      <Card>
        <ProposalForm initialLeadId={sp.get('lead')} onSaved={(p) => navigate(`/crm/propostas/${p.id}`, { replace: true })} />
      </Card>
    </div>
  )
}
