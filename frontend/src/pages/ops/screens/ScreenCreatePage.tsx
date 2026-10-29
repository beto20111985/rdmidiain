/** Adicionar tela — POST /screens (código TEL-UF-XXXX gerado pelo servidor). */
import { useSearchParams } from 'react-router-dom'
import { PageHeader } from '@/components/layout/page-meta'
import { ScreenForm } from './ScreenForm'

const CRUMBS = [{ label: 'Telas', to: '/telas' }, { label: 'Adicionar tela' }]

export default function ScreenCreatePage() {
  const [sp] = useSearchParams()
  return (
    <div className="flex flex-col gap-6">
      <PageHeader title="Adicionar tela" description="Depois do cadastro, pareie o player instalado na TV." breadcrumbs={CRUMBS} />
      <ScreenForm screen={null} establishmentId={sp.get('establishment_id')} />
    </div>
  )
}
