/** Nova empresa — POST /organizations (organizations.manage). */
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { http } from '@/lib/http'
import { OrganizationForm } from './OrganizationForm'
import type { Organization } from '../types'

export default function OrganizationNewPage() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  return (
    <>
      <PageHeader title="Nova empresa" breadcrumbs={[{ label: 'Empresas', to: '/empresas' }, { label: 'Nova empresa' }]} />
      <Card className="mx-auto w-full max-w-4xl">
        <CardHeader>
          <div className="flex flex-col gap-1">
            <CardTitle>Dados da empresa</CardTitle>
            <CardDescription>Uma empresa pode ter vários papéis ao mesmo tempo (ex.: anunciante e parceiro).</CardDescription>
          </div>
        </CardHeader>
        <OrganizationForm
          submitLabel="Cadastrar empresa"
          onCancel={() => navigate('/empresas')}
          onSubmit={async (payload) => {
            const org = await http.post<Organization>('/organizations', payload)
            await qc.invalidateQueries({ queryKey: ['organizations'] })
            toast.success(`Empresa ${org.trade_name || org.name} cadastrada.`)
            navigate(`/empresas/${org.id}`, { replace: true })
          }}
        />
      </Card>
    </>
  )
}
