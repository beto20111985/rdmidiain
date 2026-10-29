import { Link } from 'react-router-dom'
import { MapPinOff } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { EmptyState } from '@/components/patterns/states'
import { Button } from '@/components/ui/button'

export default function NotFoundPage() {
  return (
    <>
      <PageHeader title="Página não encontrada" />
      <EmptyState
        icon={MapPinOff}
        title="Este endereço não existe ou foi movido"
        description="Confira o link ou volte para o início do painel."
        action={
          <Button asChild>
            <Link to="/">Ir para o início</Link>
          </Button>
        }
      />
    </>
  )
}
