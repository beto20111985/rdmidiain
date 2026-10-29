/**
 * CRM → Equipe comercial (REQ-CRM-002/006; REQ-NEG-019): vendedores, representantes,
 * franquias e gerentes (região, remuneração, % próprio — alterar % exige commissions.adjust)
 * e comissão do vendedor por venda (atribuições criadas na conversão; pagas pelo financeiro).
 */
import * as React from 'react'
import { Controller, useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Pencil, UserPlus, Users } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Skeleton } from '@/components/ui/skeleton'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { EmptyState, ErrorState } from '@/components/patterns/states'
import { StatusPill, ToneBadge } from '@/components/finance/status-pill'
import { ATTRIBUTION_STATUS, COMPENSATION, SALES_REP_KIND } from '@/components/finance/labels'
import { bpsToInput, parsePercentToBps } from '@/components/finance/money'
import { personLabel } from '@/components/finance/names'
import { useAuthStore } from '@/stores/authStore'
import { useTableState } from '@/hooks/useTableState'
import { useCan } from '@/hooks/usePermissions'
import { http, type Paginated } from '@/lib/http'
import { qk } from '@/lib/queryKeys'
import { errorMessage, isApiError } from '@/lib/errors'
import { EMPTY_VALUE, formatBps, formatCents, formatDateTime } from '@/lib/format'
import { crmApi, type Attribution, type SalesRep } from './api'

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

const repSchema = z.object({
  user_id: z.string().trim().regex(UUID, 'Escolha o usuário.'),
  kind: z.string(),
  region_label: z.string().max(120).optional(),
  city: z.string().max(100).optional(),
  uf: z.string().optional(),
  compensation: z.string(),
  commission: z.string().refine((v) => !v || parsePercentToBps(v) !== null, 'Percentual inválido.'),
  is_active: z.boolean(),
})
type RepValues = z.infer<typeof repSchema>

function RepDialog({ rep, open, onOpenChange }: { rep: SalesRep | null; open: boolean; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient()
  const can = useCan()
  const [error, setError] = React.useState<string | null>(null)
  const users = useQuery({
    queryKey: qk.list('users', { page_size: 100, directory: true }),
    queryFn: () => http.get<Paginated<{ id: string; name: string }>>('/users', { query: { page_size: 100 } }),
    enabled: open && can('users.view'),
  })
  const { register, control, handleSubmit, reset, formState: { errors, isSubmitting } } = useForm<RepValues>({ resolver: zodResolver(repSchema) })
  React.useEffect(() => {
    if (open) {
      setError(null)
      reset({
        user_id: rep?.user_id ?? '',
        kind: rep?.kind ?? 'VENDEDOR',
        region_label: rep?.region_label ?? '',
        city: '',
        uf: rep?.uf ?? undefined,
        compensation: rep?.compensation ?? 'COMISSAO',
        commission: bpsToInput(rep?.commission_bps ?? null),
        is_active: rep?.is_active ?? true,
      })
    }
  }, [open, rep, reset])
  const onSubmit = async (v: RepValues) => {
    setError(null)
    try {
      await crmApi.upsertRep({
        user_id: v.user_id,
        kind: v.kind,
        region_label: v.region_label || undefined,
        city: v.city || undefined,
        uf: v.uf || undefined,
        compensation: v.compensation,
        commission_bps: v.commission ? parsePercentToBps(v.commission) : null,
        is_active: v.is_active,
      })
      toast.success('Representante salvo.')
      await qc.invalidateQueries({ queryKey: ['crm'] })
      onOpenChange(false)
    } catch (e) {
      setError(isApiError(e) && e.details?.permission === 'commissions.adjust' ? 'Alterar o percentual de comissão exige a permissão commissions.adjust (financeiro).' : errorMessage(e))
    }
  }
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>{rep ? 'Editar representante' : 'Adicionar à equipe comercial'}</DialogTitle>
          <DialogDescription>Região e remuneração. O % próprio substitui o padrão da instância.</DialogDescription>
        </DialogHeader>
        {error ? (
          <Alert variant="danger">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        ) : null}
        <form id="rep-form" onSubmit={handleSubmit(onSubmit)} noValidate>
          <FieldGroup>
            <Field invalid={!!errors.user_id} disabled={!!rep}>
              <FieldLabel required>Usuário</FieldLabel>
              {can('users.view') ? (
                <Controller
                  control={control}
                  name="user_id"
                  render={({ field }) => (
                    <Select value={field.value || undefined} onValueChange={field.onChange} disabled={!!rep}>
                      <SelectTrigger>
                        <SelectValue placeholder={users.isLoading ? 'Carregando…' : 'Selecione'} />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectGroup>
                          {(users.data?.items ?? []).map((u) => (
                            <SelectItem key={u.id} value={u.id}>
                              {u.name}
                            </SelectItem>
                          ))}
                        </SelectGroup>
                      </SelectContent>
                    </Select>
                  )}
                />
              ) : (
                <Input className="font-mono" {...register('user_id')} />
              )}
              {!can('users.view') ? <FieldDescription>Seu perfil não lista usuários: informe o ID do usuário.</FieldDescription> : null}
              <FieldError>{errors.user_id?.message}</FieldError>
            </Field>
            <div className="grid grid-cols-1 gap-5 sm:grid-cols-2">
              <Field>
                <FieldLabel>Tipo</FieldLabel>
                <Controller
                  control={control}
                  name="kind"
                  render={({ field }) => (
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectGroup>
                          {Object.entries(SALES_REP_KIND).map(([k, v]) => (
                            <SelectItem key={k} value={k}>
                              {v}
                            </SelectItem>
                          ))}
                        </SelectGroup>
                      </SelectContent>
                    </Select>
                  )}
                />
              </Field>
              <Field>
                <FieldLabel>Remuneração</FieldLabel>
                <Controller
                  control={control}
                  name="compensation"
                  render={({ field }) => (
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectGroup>
                          {Object.entries(COMPENSATION).map(([k, v]) => (
                            <SelectItem key={k} value={k}>
                              {v}
                            </SelectItem>
                          ))}
                        </SelectGroup>
                      </SelectContent>
                    </Select>
                  )}
                />
              </Field>
              <Field>
                <FieldLabel>Região (rótulo)</FieldLabel>
                <Input {...register('region_label')} placeholder="ex.: Goiânia e entorno" />
              </Field>
              <Field invalid={!!errors.commission}>
                <FieldLabel>Comissão própria (%)</FieldLabel>
                <Input inputMode="decimal" {...register('commission')} />
                <FieldDescription>Em branco = padrão da instância. Alterar exige commissions.adjust.</FieldDescription>
                <FieldError>{errors.commission?.message}</FieldError>
              </Field>
              <Field>
                <FieldLabel>Cidade (escopo)</FieldLabel>
                <Input {...register('city')} />
              </Field>
              <Field>
                <FieldLabel>UF</FieldLabel>
                <Input maxLength={2} className="uppercase" {...register('uf')} />
              </Field>
            </div>
            <Field orientation="horizontal" className="min-h-11">
              <Controller control={control} name="is_active" render={({ field }) => <Switch checked={field.value} onCheckedChange={field.onChange} />} />
              <FieldLabel className="font-medium">Ativo</FieldLabel>
            </Field>
          </FieldGroup>
        </form>
        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Cancelar
          </Button>
          <Button type="submit" form="rep-form" loading={isSubmitting} loadingText="Salvando…">
            Salvar
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default function SalesTeamPage() {
  const can = useCan()
  const meId = useAuthStore((s) => s.me?.user.id)
  const [dialog, setDialog] = React.useState<{ open: boolean; rep: SalesRep | null }>({ open: false, rep: null })
  const reps = useQuery({ queryKey: qk.sub('crm', 'sales-reps'), queryFn: crmApi.salesReps, retry: false })
  const table = useTableState({ defaultPageSize: 25, filterKeys: ['status'] })
  const params = { page: table.state.page, page_size: table.state.pageSize, status: table.state.filters.status }
  const attributions = useQuery({ queryKey: qk.sub('crm', 'attributions', params), queryFn: () => crmApi.attributions(params), placeholderData: keepPreviousData })
  const repsForbidden = isApiError(reps.error) && reps.error.status === 403

  const cols: DataTableColumn<Attribution>[] = [
    { id: 'date', header: 'Venda em', mobile: 'secondary', cell: (a) => formatDateTime(a.created_at) },
    { id: 'seller', header: 'Vendedor', mobile: 'primary', cell: (a) => <span className="font-semibold">{personLabel(a.seller_name, a.seller_id, meId)}</span> },
    { id: 'client', header: 'Cliente', mobile: 'field', cell: (a) => a.org_name ?? `Empresa ${a.org_id.slice(0, 8)}` },
    { id: 'amount', header: 'Valor do pedido', numeric: true, mobile: 'field', cell: (a) => formatCents(a.amount_cents) },
    { id: 'bps', header: '%', numeric: true, mobile: 'field', cell: (a) => formatBps(a.commission_bps) },
    { id: 'commission', header: 'Comissão', numeric: true, mobile: 'field', cell: (a) => formatCents(a.commission_cents) },
    { id: 'status', header: 'Situação', mobile: 'badge', cell: (a) => <StatusPill map={ATTRIBUTION_STATUS} value={a.status} /> },
  ]

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Equipe comercial"
        description="Vendedores, representantes, franquias e comissão por venda"
        breadcrumbs={[{ label: 'CRM' }, { label: 'Equipe comercial' }]}
        actions={
          can('crm.manage') && !repsForbidden ? (
            <Button onClick={() => setDialog({ open: true, rep: null })}>
              <UserPlus aria-hidden="true" />
              Adicionar
            </Button>
          ) : undefined
        }
      />
      <Card>
        <CardHeader>
          <div className="flex flex-col gap-1">
            <CardTitle>Estrutura comercial</CardTitle>
            <CardDescription>Região de atuação, gerente e forma de remuneração.</CardDescription>
          </div>
        </CardHeader>
        {reps.isLoading ? (
          <Skeleton className="h-32 w-full" />
        ) : repsForbidden ? (
          <p className="m-0 text-sm text-text-muted">A estrutura comercial completa é visível somente para a equipe com escopo nacional.</p>
        ) : reps.error ? (
          <ErrorState error={reps.error} onRetry={() => void reps.refetch()} size="compact" />
        ) : !reps.data?.items.length ? (
          <EmptyState size="compact" headingLevel="h3" icon={Users} title="Nenhum representante cadastrado." description="Sem cadastro, a comissão padrão da instância é usada para o vendedor." />
        ) : (
          <Table>
            <caption className="sr-only">Equipe comercial</caption>
            <TableHeader>
              <TableRow className="h-11 hover:bg-transparent">
                <TableHead scope="col">Pessoa</TableHead>
                <TableHead scope="col">Tipo</TableHead>
                <TableHead scope="col">Região</TableHead>
                <TableHead scope="col">Remuneração</TableHead>
                <TableHead scope="col" numeric>
                  Comissão
                </TableHead>
                <TableHead scope="col">Situação</TableHead>
                <TableHead scope="col" className="text-right">
                  Ações
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {reps.data.items.map((r) => (
                <TableRow key={r.id}>
                  <TableCell className="font-semibold">
                    {personLabel(r.user_name, r.user_id, meId)}
                    {r.manager_name ? <span className="block text-xs font-normal text-text-muted">Gerente: {r.manager_name}</span> : null}
                  </TableCell>
                  <TableCell>{SALES_REP_KIND[r.kind] ?? r.kind}</TableCell>
                  <TableCell>{r.region_label ?? (r.uf ? r.uf : EMPTY_VALUE)}</TableCell>
                  <TableCell>{COMPENSATION[r.compensation] ?? r.compensation}</TableCell>
                  <TableCell numeric>{r.commission_bps !== null ? formatBps(r.commission_bps) : 'Padrão'}</TableCell>
                  <TableCell>{r.is_active ? <ToneBadge tone="success">Ativo</ToneBadge> : <ToneBadge tone="neutral">Inativo</ToneBadge>}</TableCell>
                  <TableCell className="text-right">
                    {can('crm.manage') ? (
                      <Button variant="ghost" size="icon" aria-label={`Editar ${personLabel(r.user_name, r.user_id)}`} onClick={() => setDialog({ open: true, rep: r })}>
                        <Pencil aria-hidden="true" />
                      </Button>
                    ) : null}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Card>
      <Card>
        <CardHeader>
          <div className="flex flex-col gap-1">
            <CardTitle>Comissão de vendedores</CardTitle>
            <CardDescription>Uma atribuição por pedido convertido; % congelado na venda. Vendedor regional vê somente as próprias.</CardDescription>
          </div>
        </CardHeader>
        <DataTable
          caption="Comissão de vendedores"
          columns={cols}
          data={attributions.data}
          getRowId={(a) => a.id}
          controller={table}
          isLoading={attributions.isLoading}
          isFetching={attributions.isFetching}
          error={attributions.error}
          onRetry={() => void attributions.refetch()}
          filtersActive={!!table.state.filters.status}
          empty={{ title: 'Nenhuma venda atribuída ainda.', description: 'Atribuições nascem quando uma proposta vira pedido.' }}
          toolbar={
            <Field className="w-full max-w-[240px]">
              <FieldLabel>Situação</FieldLabel>
              <Select value={table.state.filters.status ?? 'all'} onValueChange={(v) => table.setFilter('status', v === 'all' ? null : v)}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    <SelectItem value="all">Todas</SelectItem>
                    {Object.entries(ATTRIBUTION_STATUS).map(([k, v]) => (
                      <SelectItem key={k} value={k}>
                        {v.label}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
            </Field>
          }
        />
      </Card>
      <RepDialog rep={dialog.rep} open={dialog.open} onOpenChange={(o) => setDialog((d) => ({ ...d, open: o }))} />
    </div>
  )
}
