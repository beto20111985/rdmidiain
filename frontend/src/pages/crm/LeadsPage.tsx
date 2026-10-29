/**
 * CRM → Leads (P001 §106; ETAPA2 #1024–#1027; REQ-CRM-001): busca, etapa, responsável,
 * cadastro e detalhe (atividades, histórico de etapas, propostas). Escopo regional aplicado
 * pelo servidor (vendedor de cidade vê os da sua região e os seus).
 */
import * as React from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Contact, PlusCircle } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Field, FieldLabel } from '@/components/ui/field'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { Can } from '@/components/auth/guards'
import { StatusPill } from '@/components/finance/status-pill'
import { LEAD_STAGES, LEAD_STAGE_STATUS } from '@/components/finance/labels'
import { personLabel } from '@/components/finance/names'
import { useTableState } from '@/hooks/useTableState'
import { useAuthStore } from '@/stores/authStore'
import { qk } from '@/lib/queryKeys'
import { EMPTY_VALUE, formatCents, formatDateTime } from '@/lib/format'
import { crmApi, type Lead } from './api'
import { NewLeadDialog } from './lead-dialogs'
import { LeadSheet } from './lead-sheet'

export default function LeadsPage() {
  const meId = useAuthStore((s) => s.me?.user.id)
  const table = useTableState({ defaultPageSize: 25, filterKeys: ['stage', 'owner'] })
  const [search, setSearch] = React.useState(table.state.q)
  const [newOpen, setNewOpen] = React.useState(false)
  const [openId, setOpenId] = React.useState<string | null>(null)
  const owner = table.state.filters.owner === 'me' ? meId : undefined
  const params = { page: table.state.page, page_size: table.state.pageSize, q: table.state.q || undefined, stage: table.state.filters.stage, owner_id: owner }
  const q = useQuery({ queryKey: qk.list('crm', { kind: 'leads', ...params }), queryFn: () => crmApi.leads(params), placeholderData: keepPreviousData })

  React.useEffect(() => {
    const t = window.setTimeout(() => {
      if (search !== table.state.q) table.setQuery(search)
    }, 350)
    return () => window.clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search])

  const columns: DataTableColumn<Lead>[] = [
    {
      id: 'name',
      header: 'Lead',
      mobile: 'primary',
      cell: (l) => (
        <button type="button" onClick={() => setOpenId(l.id)} className="flex min-h-11 flex-col items-start text-left">
          <span className="font-semibold text-link underline-offset-4 hover:underline">{l.name}</span>
          <span className="text-xs text-text-muted">{l.company ?? EMPTY_VALUE}</span>
        </button>
      ),
    },
    { id: 'stage', header: 'Etapa', mobile: 'badge', cell: (l) => <StatusPill map={LEAD_STAGE_STATUS} value={l.stage} /> },
    { id: 'owner', header: 'Responsável', mobile: 'field', cell: (l) => personLabel(l.owner_name, l.owner_id, meId) },
    { id: 'city', header: 'Cidade', mobile: 'field', cell: (l) => (l.city ? `${l.city}/${l.uf ?? ''}` : EMPTY_VALUE) },
    { id: 'value', header: 'Valor estimado', numeric: true, mobile: 'field', cell: (l) => formatCents(l.estimated_value_cents) },
    { id: 'created', header: 'Criado em', mobile: 'secondary', cell: (l) => formatDateTime(l.created_at) },
  ]

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Leads"
        description="Contatos comerciais, origem, responsável e próximos passos"
        actions={
          <Can permission="crm.manage">
            <Button onClick={() => setNewOpen(true)}>
              <PlusCircle aria-hidden="true" />
              Novo lead
            </Button>
          </Can>
        }
      />
      <Card>
        <DataTable
          caption="Leads"
          columns={columns}
          data={q.data}
          getRowId={(l) => l.id}
          controller={table}
          isLoading={q.isLoading}
          isFetching={q.isFetching}
          error={q.error}
          onRetry={() => void q.refetch()}
          filtersActive={!!table.state.q || Object.keys(table.state.filters).length > 0}
          empty={{ title: 'Nenhum lead cadastrado.', description: 'Cadastre o primeiro contato comercial.', icon: Contact }}
          toolbar={
            <>
              <Field className="w-full max-w-xs">
                <FieldLabel>Buscar</FieldLabel>
                <Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Nome, empresa ou e-mail" />
              </Field>
              <Field className="w-full max-w-[200px]">
                <FieldLabel>Etapa</FieldLabel>
                <Select value={table.state.filters.stage ?? 'all'} onValueChange={(v) => table.setFilter('stage', v === 'all' ? null : v)}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      <SelectItem value="all">Todas</SelectItem>
                      {LEAD_STAGES.map((s) => (
                        <SelectItem key={s.value} value={s.value}>
                          {s.label}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  </SelectContent>
                </Select>
              </Field>
              <Field className="w-full max-w-[200px]">
                <FieldLabel>Responsável</FieldLabel>
                <Select value={table.state.filters.owner ?? 'all'} onValueChange={(v) => table.setFilter('owner', v === 'all' ? null : v)}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      <SelectItem value="all">Todos</SelectItem>
                      <SelectItem value="me">Meus leads</SelectItem>
                    </SelectGroup>
                  </SelectContent>
                </Select>
              </Field>
            </>
          }
          rowActions={(l) => (
            <Button variant="secondary" onClick={() => setOpenId(l.id)} aria-label={`Abrir lead ${l.name}`}>
              Abrir
            </Button>
          )}
        />
      </Card>
      <NewLeadDialog open={newOpen} onOpenChange={setNewOpen} onCreated={(l) => setOpenId(l.id)} />
      <LeadSheet leadId={openId} onOpenChange={(o) => !o && setOpenId(null)} />
    </div>
  )
}
