/**
 * LGPD (RD, `lgpd.manage`) — fila de pedidos de exclusão (GET /lgpd/deletion-requests),
 * processamento por ANONIMIZAÇÃO (POST …/process) preservando registros legais, ou recusa
 * justificada (POST …/reject). Ninguém processa o próprio pedido; Admin Master é protegido.
 */
import * as React from 'react'
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Scale } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Field, FieldLabel } from '@/components/ui/field'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { useTableState } from '@/hooks/useTableState'
import { http, type Paginated } from '@/lib/http'
import { qk } from '@/lib/queryKeys'
import { formatDateTime } from '@/lib/format'
import { DELETION_STATUS, lookup } from '@/components/admin/labels'
import { ReasonDialog, ToneBadge } from '@/components/admin/ui'

interface DeletionRequest {
  id: string
  user_id: string
  user_name: string
  status: string
  reason: string | null
  requested_at: string
  processed_at: string | null
}

export default function LgpdAdminPage() {
  const qc = useQueryClient()
  const table = useTableState({ defaultPageSize: 25, filterKeys: ['status'] })
  const status = table.state.filters.status ?? 'PENDING'
  const params = { page: table.state.page, page_size: table.state.pageSize, status: status === 'ALL' ? undefined : status }
  const q = useQuery({
    queryKey: qk.list('settings', { lgpd: params }),
    queryFn: () => http.get<Paginated<DeletionRequest>>('/lgpd/deletion-requests', { query: params }),
    placeholderData: keepPreviousData,
  })
  const [processing, setProcessing] = React.useState<DeletionRequest | null>(null)
  const [rejecting, setRejecting] = React.useState<DeletionRequest | null>(null)
  const invalidate = () => void qc.invalidateQueries({ queryKey: ['settings', 'list'] })

  const columns: DataTableColumn<DeletionRequest>[] = [
    { id: 'user', header: 'Titular', mobile: 'primary', cell: (r) => <span className="font-semibold">{r.user_name}</span> },
    { id: 'status', header: 'Situação', mobile: 'badge', cell: (r) => <ToneBadge value={lookup(DELETION_STATUS, r.status)} /> },
    { id: 'reason', header: 'Motivo informado', mobile: 'field', cell: (r) => r.reason || '—' },
    { id: 'requested', header: 'Pedido em', mobile: 'secondary', cell: (r) => <span className="tabular-nums">{formatDateTime(r.requested_at)}</span> },
    { id: 'processed', header: 'Processado em', mobile: 'field', cell: (r) => <span className="tabular-nums">{formatDateTime(r.processed_at)}</span> },
  ]

  return (
    <>
      <PageHeader title="LGPD" description="Pedidos de exclusão de titulares: anonimização com preservação dos registros exigidos por lei." />
      <Card>
        <DataTable
          caption="Pedidos de exclusão"
          columns={columns}
          data={q.data}
          getRowId={(r) => r.id}
          controller={table}
          isLoading={q.isLoading}
          isFetching={q.isFetching}
          error={q.error}
          onRetry={() => void q.refetch()}
          filtersActive={status !== 'PENDING'}
          empty={{ icon: Scale, title: 'Nenhum pedido pendente.', description: 'Pedidos feitos pelos titulares em Minha conta → Privacidade aparecem aqui.' }}
          rowActions={(r) =>
            r.status === 'PENDING' ? (
              <>
                <Button variant="secondary" onClick={() => setProcessing(r)}>
                  Anonimizar
                </Button>
                <Button variant="ghost" onClick={() => setRejecting(r)}>
                  Recusar
                </Button>
              </>
            ) : null
          }
          toolbar={
            <Field className="w-full sm:w-56">
              <FieldLabel>Situação</FieldLabel>
              <Select value={status} onValueChange={(v) => table.setFilter('status', v === 'PENDING' ? null : v)}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    <SelectItem value="PENDING">Pendentes</SelectItem>
                    <SelectItem value="COMPLETED">Anonimizados</SelectItem>
                    <SelectItem value="REJECTED">Recusados</SelectItem>
                    <SelectItem value="ALL">Todos</SelectItem>
                  </SelectGroup>
                </SelectContent>
              </Select>
            </Field>
          }
        />
      </Card>
      <ReasonDialog
        open={!!processing}
        onOpenChange={(o) => !o && setProcessing(null)}
        title={`Anonimizar os dados de ${processing?.user_name ?? ''}?`}
        description="Ação irreversível, registrada na auditoria."
        consequences={[
          'Nome, e-mail e telefone são substituídos; senha, 2FA e códigos de recuperação são invalidados.',
          'Sessões são encerradas, perfis removidos e a conta fica inativa.',
          'Preservados (obrigação legal): registros financeiros, trilha de auditoria e consentimentos — vinculados ao identificador pseudonimizado.',
        ]}
        reasonLabel="Observações (opcional)"
        minLength={0}
        maxLength={2000}
        requireText={processing?.user_name}
        confirmLabel="Anonimizar titular"
        destructive
        onConfirm={async (notes) => {
          if (!processing) return
          await http.post(`/lgpd/deletion-requests/${processing.id}/process`, { notes: notes || null })
          invalidate()
          toast.success('Titular anonimizado.')
        }}
      />
      <ReasonDialog
        open={!!rejecting}
        onOpenChange={(o) => !o && setRejecting(null)}
        title={`Recusar o pedido de ${rejecting?.user_name ?? ''}?`}
        description="Informe a base legal ou o motivo da recusa; o registro fica na auditoria."
        reasonLabel="Justificativa"
        minLength={10}
        maxLength={2000}
        confirmLabel="Recusar pedido"
        destructive
        onConfirm={async (notes) => {
          if (!rejecting) return
          await http.post(`/lgpd/deletion-requests/${rejecting.id}/reject`, { notes })
          invalidate()
          toast.success('Pedido recusado.')
        }}
      />
    </>
  )
}
