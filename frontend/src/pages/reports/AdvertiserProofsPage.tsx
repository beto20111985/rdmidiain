/** Minhas campanhas → Comprovantes de veiculação (PDF assinado; somente exibições válidas). */
import { Megaphone } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { EmptyState } from '@/components/patterns/states'
import { useContextOrgId } from '@/components/finance/hooks'
import { ProofsPanel } from './proofs-panel'

export default function AdvertiserProofsPage() {
  const orgId = useContextOrgId()
  return (
    <div className="flex flex-col gap-6">
      <PageHeader title="Comprovantes de veiculação" description="PDF assinado com as exibições válidas das suas campanhas" />
      {orgId ? (
        <ProofsPanel orgId={orgId} />
      ) : (
        <EmptyState icon={Megaphone} title="Selecione a empresa anunciante" description="Use o seletor de contexto para entrar em “Minhas campanhas”." />
      )}
    </div>
  )
}
