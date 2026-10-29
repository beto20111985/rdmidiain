/**
 * Campanhas (PG-08; DS §13): lista por contexto — a equipe RD vê todas (por escopo), o
 * anunciante/agência só as suas (o servidor filtra pelo escopo). Status PT com filtros,
 * busca, ordenação e paginação na URL; busca pelo código do anúncio RD-XXXXXX (PG-47).
 * Atualização em tempo real por `campaign.updated` (sem polling).
 */
import * as React from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { keepPreviousData, useMutation, useQuery } from '@tanstack/react-query'
import { Plus, Search, ShoppingCart } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { StaleData } from '@/components/patterns/states'
import { Can } from '@/components/auth/guards'
import { CampaignStatusBadge, PlayingBadge } from '@/components/campaigns/badges'
import { FilterSelect } from '@/components/campaigns/filter-select'
import { OrgName } from '@/components/campaigns/org-name'
import { useTableState } from '@/hooks/useTableState'
import { useRealtimeStatus } from '@/lib/realtime/context'
import { qk } from '@/lib/queryKeys'
import { formatCents, formatNumber } from '@/lib/format'
import { campaignsApi } from './lib/api'
import { CAMPAIGN_STATUS, CAMPAIGN_STATUS_ORDER, campaignStatusLabel } from './lib/labels'
import { periodText } from './lib/format'
import { useCampaignArea } from './lib/hooks'
import type { Campaign } from './lib/types'
import { describeError } from '@/pages/campaigns/lib/errorText'

export default function CampaignsListPage() {
  const area = useCampaignArea()
  const { live } = useRealtimeStatus()
  const table = useTableState({ defaultPageSize: 25, defaultSort: '-created_at', filterKeys: ['status'] })
  const q = useQuery({
    queryKey: qk.list('campaigns', { ...table.apiParams, area: area.isStaff ? 'rd' : 'adv' }),
    queryFn: () => campaignsApi.list(table.apiParams),
    placeholderData: keepPreviousData,
  })

  const cols: DataTableColumn<Campaign>[] = [
    {
      id: 'name',
      header: 'Campanha',
      sortField: 'name',
      mobile: 'primary',
      cell: (c) => (
        <Link className="font-semibold text-link underline-offset-4 [overflow-wrap:anywhere] hover:underline" to={`${area.campaignsBase}/${c.id}`}>
          {c.name}
        </Link>
      ),
    },
    ...(area.isStaff
      ? [{ id: 'org', header: 'Anunciante', mobile: 'secondary' as const, cell: (c: Campaign) => <OrgName id={c.org_id} /> }]
      : []),
    {
      id: 'status',
      header: 'Status',
      mobile: 'badge',
      cell: (c) => (
        <span className="flex flex-col items-start gap-1">
          <span className="flex flex-wrap gap-1">
            <CampaignStatusBadge status={c.status} />
            <PlayingBadge campaign={c} />
          </span>
          {c.has_pending_change ? <span className="text-xs text-text-muted">Alteração ou renovação pendente (versão vigente no ar)</span> : null}
        </span>
      ),
    },
    { id: 'period', header: 'Vigência', sortField: 'start_date', cell: (c) => periodText(c) },
    { id: 'duration', header: 'Duração', numeric: true, cell: (c) => `${c.duration_s} s` },
    {
      id: 'price',
      header: 'Valor contratado',
      numeric: true,
      cell: (c) => (c.contracted_price_cents > 0 ? formatCents(c.contracted_price_cents) : '—'),
    },
  ]

  const filtersActive = !!table.state.q || Object.keys(table.state.filters).length > 0
  const actions = (
    <div className="flex flex-wrap gap-2">
      {!area.isStaff ? (
        <Can permission="checkout.create">
          <Button variant="secondary" asChild>
            <Link to={area.cartPath}>
              <ShoppingCart aria-hidden="true" />
              Carrinho
            </Link>
          </Button>
        </Can>
      ) : null}
      <Can permission="campaigns.manage">
        <Button asChild>
          <Link to={area.newCampaignPath}>
            <Plus aria-hidden="true" />
            Nova campanha
          </Link>
        </Button>
      </Can>
    </div>
  )

  return (
    <>
      <PageHeader
        title="Campanhas"
        description={q.data ? `${formatNumber(q.data.total)} campanha(s)${area.isStaff ? '' : ' da sua organização'}` : undefined}
        actions={actions}
      />
      <div className="flex flex-col gap-6">
        {!live && q.data ? (
          <StaleData updatedAt={q.dataUpdatedAt} reason="tempo real indisponível; os status podem estar desatualizados." onRefresh={() => void q.refetch()} refreshing={q.isFetching} />
        ) : null}
        <Card>
          <DataTable
            caption="Campanhas"
            columns={cols}
            data={q.data}
            getRowId={(c) => c.id}
            controller={table}
            isLoading={q.isLoading}
            isFetching={q.isFetching}
            error={q.error}
            onRetry={() => void q.refetch()}
            filtersActive={filtersActive}
            empty={{
              title: 'Nenhuma campanha ainda',
              description: 'Crie uma campanha: escolha os anúncios, onde e quando exibir, e veja o preço calculado na hora.',
              action: (
                <Can permission="campaigns.manage">
                  <Button asChild>
                    <Link to={area.newCampaignPath}>Criar campanha</Link>
                  </Button>
                </Can>
              ),
            }}
            toolbar={
              <>
                <Field className="max-w-xs">
                  <FieldLabel>Buscar</FieldLabel>
                  <Input type="search" value={table.state.q} onChange={(e) => table.setQuery(e.target.value)} placeholder="Nome da campanha" />
                </Field>
                <FilterSelect
                  label="Status"
                  value={table.state.filters.status}
                  onChange={(v) => table.setFilter('status', v)}
                  options={CAMPAIGN_STATUS_ORDER.map((s) => ({ value: s, label: CAMPAIGN_STATUS[s].label }))}
                />
              </>
            }
          />
        </Card>
        <Can permission="creatives.view">
          <CodeLookup campaignsBase={area.campaignsBase} />
        </Can>
      </div>
    </>
  )
}

/** Localiza criativo/campanha pelo código exibido na TV (RD-XXXXXX). */
function CodeLookup({ campaignsBase }: { campaignsBase: string }) {
  const navigate = useNavigate()
  const [code, setCode] = React.useState('')
  const [formatError, setFormatError] = React.useState<string | null>(null)
  const lookup = useMutation({ mutationFn: (c: string) => campaignsApi.byCode(c) })
  return (
    <Card>
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault()
          const c = code.trim().toUpperCase()
          if (!/^RD-[A-Z0-9]{6}$/.test(c)) {
            setFormatError('Use o formato RD-XXXXXX.')
            return
          }
          setFormatError(null)
          lookup.mutate(c)
        }}
      >
        <Field invalid={!!formatError} className="max-w-xs">
          <FieldLabel>Buscar pelo código do anúncio</FieldLabel>
          <Input value={code} onChange={(e) => setCode(e.target.value)} placeholder="RD-XXXXXX" autoComplete="off" />
          <FieldDescription>O código aparece na tela durante a exibição.</FieldDescription>
          <FieldError>{formatError}</FieldError>
        </Field>
        <Button type="submit" variant="secondary" loading={lookup.isPending}>
          <Search aria-hidden="true" />
          Localizar
        </Button>
      </form>
      {lookup.error ? <p role="alert" className="m-0 text-sm text-danger">{describeError(lookup.error)}</p> : null}
      {lookup.data ? (
        <div className="flex flex-col gap-2 text-sm" aria-live="polite">
          <p className="m-0">
            <strong>{lookup.data.creative.code}</strong> — {lookup.data.creative.name} ({lookup.data.creative.duration_seconds} s)
          </p>
          {lookup.data.campaigns.length ? (
            <ul className="m-0 flex list-none flex-col gap-1 p-0">
              {lookup.data.campaigns.map((c) => (
                <li key={c.id}>
                  <Button variant="tertiary" onClick={() => navigate(`${campaignsBase}/${c.id}`)}>
                    {c.name} · {campaignStatusLabel(c.status)}
                  </Button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="m-0 text-text-muted">Anúncio sem campanha vinculada no seu acesso.</p>
          )}
        </div>
      ) : null}
    </Card>
  )
}
