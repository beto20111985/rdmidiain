/** Editar tela — PATCH /screens/{id}. */
import { useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { PageHeader } from '@/components/layout/page-meta'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { ErrorState, NoPermission, PageSkeleton } from '@/components/patterns/states'
import { isApiError } from '@/lib/errors'
import { qk } from '@/lib/queryKeys'
import { screensApi } from '@/pages/ops/api/client'
import { ScreenForm } from './ScreenForm'

export default function ScreenEditPage() {
  const { id = '' } = useParams()
  const q = useQuery({ queryKey: qk.detail('screens', id), queryFn: () => screensApi.get(id) })
  const code = q.data?.friendly_id
  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title={code ? `Editar ${code}` : 'Editar tela'}
        breadcrumbs={[{ label: 'Telas', to: '/telas' }, { label: code ?? 'Tela', to: `/telas/${id}` }, { label: 'Editar' }]}
      />
      {q.isLoading ? (
        <PageSkeleton />
      ) : isApiError(q.error) && q.error.isForbidden ? (
        <NoPermission permission="screens.manage" />
      ) : q.error || !q.data ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />
      ) : q.data.status === 'DECOMMISSIONED' ? (
        <Alert variant="warning">
          <AlertDescription className="text-text">Tela retirada definitivamente — não pode ser editada.</AlertDescription>
        </Alert>
      ) : (
        <ScreenForm key={q.data.id} screen={q.data} />
      )}
    </div>
  )
}
