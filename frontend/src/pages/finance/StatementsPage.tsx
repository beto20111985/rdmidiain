/**
 * Financeiro → Extratos (P001 §79, §91): balancete por conta do ledger e extrato por conta ou
 * por organização (anunciante: receita diferida/reembolsos; estabelecimento: comissões).
 * Saldos são sempre a soma das partidas (nunca coluna mutável).
 */
import * as React from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { ScrollText } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Field, FieldLabel } from '@/components/ui/field'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { EmptyState } from '@/components/patterns/states'
import { PeriodSelector } from '@/components/finance/period-selector'
import { formatIsoDate, usePeriodState } from '@/components/finance/period'
import { ACCOUNT_KIND_LABEL, LEDGER_KIND_LABEL, OWNER_TYPE_LABEL } from '@/components/finance/labels'
import { useOrgNames } from '@/components/finance/hooks'
import { useTableState } from '@/hooks/useTableState'
import { qk } from '@/lib/queryKeys'
import { formatCents } from '@/lib/format'
import { cn } from '@/lib/utils'
import { financeApi, type StatementItem, type StatementResponse, type TrialBalanceRow } from './api'

function statementColumns(): DataTableColumn<StatementItem>[] {
  return [
    { id: 'date', header: 'Competência', mobile: 'secondary', cell: (s) => formatIsoDate(s.competence_date) },
    {
      id: 'desc',
      header: 'Lançamento',
      mobile: 'primary',
      cell: (s) => (
        <div className="flex flex-col gap-0.5">
          <span className="font-semibold">{s.description}</span>
          <span className="text-xs text-text-muted">
            {LEDGER_KIND_LABEL[s.kind] ?? s.kind}
            {s.reverses_transaction_id ? ' · estorno de lançamento anterior' : ''}
            {s.memo ? ` · ${s.memo}` : ''}
          </span>
        </div>
      ),
    },
    { id: 'account', header: 'Conta', mobile: 'field', cell: (s) => <span className="font-mono text-xs">{s.account_code}</span> },
    {
      id: 'amount',
      header: 'Valor',
      numeric: true,
      mobile: 'field',
      cell: (s) => <span className={cn(s.natural_amount_cents < 0 && 'text-danger')}>{formatCents(s.natural_amount_cents)}</span>,
    },
  ]
}

function Balances({ data }: { data: StatementResponse | undefined }) {
  const entries = Object.entries(data?.balances ?? {})
  if (!entries.length) return null
  return (
    <ul className="m-0 flex list-none flex-col gap-1 p-0 text-sm">
      {entries.map(([k, v]) => (
        <li key={k} className="flex flex-wrap justify-between gap-2">
          <span className="font-mono text-xs">{k.split('|')[0]}</span>
          <strong className="tabular-nums">{formatCents(v)}</strong>
        </li>
      ))}
    </ul>
  )
}

function AccountStatement({ account }: { account: TrialBalanceRow }) {
  const { period, setPeriod } = usePeriodState({ defaultPreset: '30', prefix: 'a_' })
  const table = useTableState({ defaultPageSize: 50, prefix: 'a_' })
  const accountId = (account.account_id ?? account.id) as string
  const params = { start: period.start, end: period.end, page: table.state.page, page_size: table.state.pageSize }
  const q = useQuery({
    queryKey: qk.sub('finance', 'statement', 'account', accountId, params),
    queryFn: () => financeApi.accountStatement(accountId, params),
    placeholderData: keepPreviousData,
  })
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>
            Extrato · <span className="font-mono">{account.code}</span>
          </CardTitle>
          <CardDescription>Saldo atual (natureza da conta): {formatCents(account.balance_cents ?? null)}</CardDescription>
        </div>
      </CardHeader>
      <PeriodSelector value={period} onChange={setPeriod} />
      <DataTable
        caption={`Extrato da conta ${account.code}`}
        columns={statementColumns()}
        data={q.data}
        getRowId={(s) => s.posting_id}
        controller={table}
        isLoading={q.isLoading}
        isFetching={q.isFetching}
        error={q.error}
        onRetry={() => void q.refetch()}
        empty={{ title: 'Nenhum lançamento neste período.' }}
      />
    </Card>
  )
}

function AccountsTab() {
  const [code, setCode] = React.useState('')
  const [applied, setApplied] = React.useState('')
  const [selected, setSelected] = React.useState<TrialBalanceRow | null>(null)
  const table = useTableState({ defaultPageSize: 100, prefix: 'tb_' })
  const q = useQuery({
    queryKey: qk.sub('finance', 'accounts', applied),
    queryFn: () => financeApi.accounts({ code: applied || undefined }),
  })
  // Balancete vem completo (sem paginação no servidor): paginamos localmente.
  const all = q.data?.items ?? []
  const start = (table.state.page - 1) * table.state.pageSize
  const pageData = q.data ? { items: all.slice(start, start + table.state.pageSize), total: all.length, page: table.state.page, page_size: table.state.pageSize } : undefined

  const columns: DataTableColumn<TrialBalanceRow>[] = [
    { id: 'code', header: 'Conta', mobile: 'primary', cell: (r) => <span className="font-mono text-xs">{r.code}</span> },
    { id: 'owner', header: 'Titular', mobile: 'field', cell: (r) => (r.owner_id ? <span className="text-xs">{OWNER_TYPE_LABEL[String(r.owner_type ?? '')] ?? String(r.owner_type ?? '')} <span className="font-mono">{r.owner_id.slice(0, 8)}</span></span> : 'RD') },
    { id: 'kind', header: 'Natureza', mobile: 'field', cell: (r) => ACCOUNT_KIND_LABEL[String(r.kind ?? '')] ?? String(r.kind ?? '—') },
    { id: 'balance', header: 'Saldo', numeric: true, mobile: 'field', cell: (r) => formatCents(r.balance_cents ?? null) },
  ]

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(e) => {
            e.preventDefault()
            setApplied(code.trim())
            table.setPage(1)
          }}
        >
          <Field className="w-full max-w-xs">
            <FieldLabel>Filtrar por código (prefixo)</FieldLabel>
            <Input value={code} onChange={(e) => setCode(e.target.value)} placeholder="ex.: RD:Cash" />
          </Field>
          <Button type="submit" variant="secondary">
            Filtrar
          </Button>
        </form>
        {q.data ? (
          <p className="m-0 text-xs text-text-muted">
            Verificação do balancete (soma das partidas): {formatCents(q.data.check_sum_cents)} {q.data.check_sum_cents === 0 ? '— equilibrado.' : '— atenção: diferente de zero.'}
          </p>
        ) : null}
        <DataTable
          caption="Balancete"
          columns={columns}
          data={pageData}
          getRowId={(r) => String(r.account_id ?? r.code)}
          controller={table}
          isLoading={q.isLoading}
          isFetching={q.isFetching}
          error={q.error}
          onRetry={() => void q.refetch()}
          filtersActive={!!applied}
          empty={{ title: 'Nenhuma conta no ledger ainda.' }}
          rowActions={(r) => (
            <Button variant="secondary" onClick={() => setSelected(r)} aria-label={`Ver extrato da conta ${r.code}`}>
              Extrato
            </Button>
          )}
        />
      </Card>
      {selected ? <AccountStatement account={selected} /> : null}
    </div>
  )
}

function OrganizationTab() {
  const orgs = useOrgNames()
  const [orgId, setOrgId] = React.useState<string | undefined>(undefined)
  const { period, setPeriod } = usePeriodState({ defaultPreset: '30', prefix: 'o_' })
  const table = useTableState({ defaultPageSize: 50, prefix: 'o_' })
  const params = { start: period.start, end: period.end, page: table.state.page, page_size: table.state.pageSize }
  const q = useQuery({
    queryKey: qk.sub('finance', 'statement', 'org', orgId ?? '', params),
    queryFn: () => financeApi.orgStatement(orgId as string, params),
    enabled: !!orgId,
    placeholderData: keepPreviousData,
  })
  return (
    <Card>
      <div className="flex flex-wrap items-end gap-6">
        <Field className="w-full max-w-sm">
          <FieldLabel>Organização</FieldLabel>
          <Select value={orgId} onValueChange={setOrgId}>
            <SelectTrigger>
              <SelectValue placeholder="Selecione a empresa" />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                {orgs.items.map((o) => (
                  <SelectItem key={o.id} value={o.id}>
                    {o.trade_name || o.name}
                  </SelectItem>
                ))}
              </SelectGroup>
            </SelectContent>
          </Select>
        </Field>
        <PeriodSelector value={period} onChange={setPeriod} />
      </div>
      {!orgId ? (
        <EmptyState size="compact" headingLevel="h3" icon={ScrollText} title="Escolha uma organização" description="Anunciantes: receita diferida e reembolsos. Estabelecimentos: comissões, saques e saldo." />
      ) : (
        <>
          <Balances data={q.data} />
          <DataTable
            caption={`Extrato de ${orgs.nameOf(orgId)}`}
            columns={statementColumns()}
            data={q.data}
            getRowId={(s) => s.posting_id}
            controller={table}
            isLoading={q.isLoading}
            isFetching={q.isFetching}
            error={q.error}
            onRetry={() => void q.refetch()}
            empty={{ title: 'Nenhum lançamento desta organização no período.' }}
          />
        </>
      )}
    </Card>
  )
}

export default function StatementsPage() {
  return (
    <div className="flex flex-col gap-6">
      <PageHeader title="Extratos" description="Ledger em partidas dobradas, por conta e por organização" breadcrumbs={[{ label: 'Financeiro', to: '/financeiro' }, { label: 'Extratos' }]} />
      <Tabs defaultValue="contas">
        <TabsList>
          <TabsTrigger value="contas">Por conta</TabsTrigger>
          <TabsTrigger value="organizacao">Por organização</TabsTrigger>
        </TabsList>
        <TabsContent value="contas">
          <AccountsTab />
        </TabsContent>
        <TabsContent value="organizacao">
          <OrganizationTab />
        </TabsContent>
      </Tabs>
    </div>
  )
}
