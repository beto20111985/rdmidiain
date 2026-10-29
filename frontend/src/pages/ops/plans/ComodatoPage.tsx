/**
 * Comodato (RD): acordos de TV da RD cedida a estabelecimentos — lista com filtros, novo acordo e
 * detalhe (itens em comodato vinculados no Estoque). GET/POST /comodato-agreements, POST /{id}/end.
 */
import * as React from 'react'
import { Link } from 'react-router-dom'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Handshake, Plus } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Can } from '@/components/auth/guards'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { NoPermission } from '@/components/patterns/states'
import { EstablishmentName, ScreenCode } from '@/components/ops/entity-name'
import { FilterSelect } from '@/components/ops/filter-select'
import { ReasonDialog } from '@/components/ops/reason-dialog'
import { useTableState } from '@/hooks/useTableState'
import { useCan } from '@/hooks/usePermissions'
import { isApiError } from '@/lib/errors'
import { qk } from '@/lib/queryKeys'
import { formatCents, formatDate } from '@/lib/format'
import { comodatoApi } from '@/pages/ops/api/client'
import { UPGRADE_POLICY_LABEL, labelOf } from '@/pages/ops/api/labels'
import type { ComodatoAgreement } from '@/pages/ops/api/types'
import { ComodatoFormDialog } from './ComodatoFormDialog'

export default function ComodatoPage() {
  const can = useCan()
  const qc = useQueryClient()
  const table = useTableState({ defaultPageSize: 25, filterKeys: ['status'] })
  const params = { page: table.state.page, page_size: table.state.pageSize, status: table.state.filters.status }
  const q = useQuery({
    queryKey: qk.sub('establishments', 'comodato', 'list', params),
    queryFn: () => comodatoApi.list(params),
    placeholderData: keepPreviousData,
  })
  const [open, setOpen] = React.useState(false)
  const [ending, setEnding] = React.useState<ComodatoAgreement | null>(null)
  const end = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) => comodatoApi.end(id, reason),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['establishments'] })
      toast.success('Acordo encerrado.')
    },
  })

  if (isApiError(q.error) && q.error.isForbidden) return <NoPermission permission="establishments.view" />

  const columns: DataTableColumn<ComodatoAgreement>[] = [
    {
      id: 'est',
      header: 'Estabelecimento',
      mobile: 'primary',
      cell: (a) => <EstablishmentName id={a.establishment_id} name={a.establishment_name} />,
    },
    { id: 'screen', header: 'Tela', cell: (a) => (a.screen_id ? <ScreenCode id={a.screen_id} /> : 'Todas/sem tela') },
    {
      id: 'status',
      header: 'Status',
      mobile: 'badge',
      cell: (a) => <Badge tone={a.status === 'ACTIVE' ? 'success' : 'neutral'}>{a.status === 'ACTIVE' ? 'Ativo' : 'Encerrado'}</Badge>,
    },
    { id: 'since', header: 'Desde', cell: (a) => formatDate(a.started_at) },
    { id: 'deposit', header: 'Caução', numeric: true, cell: (a) => (a.deposit_required ? formatCents(a.deposit_cents) : 'Não') },
    {
      id: 'install',
      header: 'Instalação',
      numeric: true,
      cell: (a) => (a.installation_fee_required ? formatCents(a.installation_fee_cents) : 'Gratuita'),
    },
    { id: 'upgrade', header: 'Upgrade', cell: (a) => labelOf(UPGRADE_POLICY_LABEL, a.upgrade_policy) },
  ]

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Comodato"
        description="TVs e acessórios da RD cedidos aos estabelecimentos. Itens são movimentados no Estoque (tipo Comodato)."
        actions={
          <Can permission="establishments.manage">
            <Button onClick={() => setOpen(true)}>
              <Plus aria-hidden="true" />
              Novo acordo
            </Button>
          </Can>
        }
      />
      <Card>
        <DataTable
          caption="Acordos de comodato"
          columns={columns}
          data={q.data}
          getRowId={(a) => a.id}
          controller={table}
          isLoading={q.isLoading}
          isFetching={q.isFetching}
          error={q.error}
          onRetry={() => void q.refetch()}
          filtersActive={!!table.state.filters.status}
          rowActions={(a) =>
            a.status === 'ACTIVE' && can('establishments.manage') ? (
              <Button variant="tertiary" onClick={() => setEnding(a)}>
                Encerrar
              </Button>
            ) : (
              <Button asChild variant="tertiary">
                <Link to={`/estabelecimentos/${a.establishment_id}?aba=contratos`}>Ver</Link>
              </Button>
            )
          }
          empty={{ icon: Handshake, title: 'Nenhum acordo de comodato.', description: 'Registre um acordo quando a RD ceder a TV ao estabelecimento.' }}
          toolbar={
            <FilterSelect
              label="Status"
              value={table.state.filters.status}
              onChange={(v) => table.setFilter('status', v)}
              options={[
                { value: 'ACTIVE', label: 'Ativos' },
                { value: 'ENDED', label: 'Encerrados' },
              ]}
            />
          }
        />
      </Card>
      <ComodatoFormDialog open={open} onOpenChange={setOpen} />
      <ReasonDialog
        open={!!ending}
        onOpenChange={(o) => !o && setEnding(null)}
        title="Encerrar acordo de comodato?"
        consequences={['Só é possível encerrar depois que todos os equipamentos em comodato forem devolvidos ao estoque.']}
        confirmLabel="Encerrar acordo"
        destructive
        onConfirm={(reason) => end.mutateAsync({ id: ending!.id, reason })}
      />
    </div>
  )
}
