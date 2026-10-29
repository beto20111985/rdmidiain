/** Editar estabelecimento — PATCH /establishments/{id}. */
import { useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { PageHeader } from '@/components/layout/page-meta'
import { ErrorState, NoPermission, PageSkeleton } from '@/components/patterns/states'
import { isApiError } from '@/lib/errors'
import { qk } from '@/lib/queryKeys'
import { establishmentsApi } from '@/pages/ops/api/client'
import { EstablishmentForm } from './EstablishmentForm'

export default function EstablishmentEditPage() {
  const { id = '' } = useParams()
  const q = useQuery({ queryKey: qk.detail('establishments', id), queryFn: () => establishmentsApi.get(id) })
  const name = q.data?.name
  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title={name ? `Editar ${name}` : 'Editar estabelecimento'}
        breadcrumbs={[
          { label: 'Estabelecimentos', to: '/estabelecimentos' },
          { label: name ?? 'Estabelecimento', to: `/estabelecimentos/${id}` },
          { label: 'Editar' },
        ]}
      />
      {q.isLoading ? (
        <PageSkeleton />
      ) : isApiError(q.error) && q.error.isForbidden ? (
        <NoPermission permission="establishments.manage" />
      ) : q.error || !q.data ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />
      ) : (
        <EstablishmentForm key={q.data.id} establishment={q.data} />
      )}
    </div>
  )
}
