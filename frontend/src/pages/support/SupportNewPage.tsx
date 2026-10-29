/**
 * Abrir chamado — rota reutilizável por contexto (/suporte/novo, /unidade/suporte/novo,
 * /anunciante/suporte/novo). Parâmetros: ?codigo=RD-XXXXXX, ?categoria=…, ?tela=<id>,
 * ?estabelecimento=<id> (links do FE-OPS: "Pedir retirada de anúncio").
 */
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useActiveContext, useActiveRouteContext } from '@/stores/contextStore'
import { OpenTicketForm } from '@/components/admin/open-ticket-form'
import { SUPPORT_BASE } from '@/components/admin/support-links'

export default function SupportNewPage() {
  const [sp] = useSearchParams()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const ctx = useActiveRouteContext() ?? 'rd'
  const info = useActiveContext()
  const base = SUPPORT_BASE[ctx]
  const category = sp.get('categoria')
  return (
    <>
      <PageHeader title="Abrir chamado" breadcrumbs={[{ label: 'Suporte', to: base }, { label: 'Abrir chamado' }]} />
      <Card className="mx-auto w-full max-w-3xl">
        <CardHeader>
          <div className="flex flex-col gap-1">
            <CardTitle>{category === 'RETIRADA_ANUNCIO' ? 'Pedido de retirada de anúncio' : 'Novo chamado'}</CardTitle>
            <CardDescription>A equipe RD responde por aqui; você acompanha o andamento e o prazo na lista de chamados.</CardDescription>
          </div>
        </CardHeader>
        <OpenTicketForm
          context={ctx}
          orgId={info?.org_id ?? null}
          initial={{
            category,
            creativeCode: sp.get('codigo'),
            screenId: sp.get('tela'),
            establishmentId: sp.get('estabelecimento'),
          }}
          onCancel={() => navigate(base)}
          onCreated={(t) => {
            void qc.invalidateQueries({ queryKey: ['support'] })
            toast.success(`Chamado ${t.number} aberto.`)
            navigate(`${base}/${t.id}`, { replace: true })
          }}
        />
      </Card>
    </>
  )
}
