/** Novo estabelecimento (cadastro direto pela equipe RD) — POST /establishments. */
import { Link } from 'react-router-dom'
import { ClipboardList } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Button } from '@/components/ui/button'
import { EstablishmentForm } from './EstablishmentForm'

const CRUMBS = [{ label: 'Estabelecimentos', to: '/estabelecimentos' }, { label: 'Novo estabelecimento' }]

export default function EstablishmentCreatePage() {
  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Novo estabelecimento"
        description="Cadastro direto. Para o fluxo guiado com aceite do contrato e instalação, use Cadastros de parceiros."
        breadcrumbs={CRUMBS}
        actions={
          <Button asChild variant="secondary">
            <Link to="/estabelecimentos/cadastros">
              <ClipboardList aria-hidden="true" />
              Cadastro guiado
            </Link>
          </Button>
        }
      />
      <EstablishmentForm establishment={null} />
    </div>
  )
}
