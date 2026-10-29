/**
 * Etapa 3 — Telas e locais por SEGMENTAÇÃO (nunca TV individual), com disponibilidade em
 * tempo real calculada pelo servidor a cada mudança (contagem por região/UF/cidade — sem
 * nomes de estabelecimentos) e explicação de conflitos (RD: decisão e caminho das regras;
 * anunciante: apenas contagens).
 */
import * as React from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Skeleton } from '@/components/ui/skeleton'
import { TargetingEditor } from '@/components/campaigns/targeting-editor'
import { RulePath } from '@/components/campaigns/rule-path'
import { EstablishmentName } from '@/components/campaigns/org-name'
import { qk } from '@/lib/queryKeys'
import { formatNumber } from '@/lib/format'
import { campaignsApi, taxonomyApi } from '../lib/api'
import { conflictExclusionText, OUT_OF_INVENTORY_TEXT, regionLabel } from '../lib/labels'
import { cityKeyLabel, serverFieldErrors } from '../lib/rules'
import { useDebounced } from '../lib/hooks'
import type { AvailabilityExclusions, AvailabilitySummary, Target } from '../lib/types'
import { ServerError, StepActions, type StepProps } from './shared'
import { describeError } from '@/pages/campaigns/lib/errorText'

/** "1 tela está" / "3 telas estão" etc. */
function plural(n: number, one: string, many: string): string {
  return `${formatNumber(n)} ${n === 1 ? one : many}`
}

function cityLabel(r: { city_key: string; city?: string | null }): string {
  const uf = r.city_key.split(':')[0]
  return r.city ? `${r.city} (${uf})` : cityKeyLabel(r.city_key)
}

/**
 * SOMENTE equipe RD: por que telas que casam a segmentação ficaram de fora (contagens por motivo, sem
 * nomes) e o próximo passo. As regras não mudam — só deixam de ser silenciosas (AÇÃO 004, item 14).
 */
function ExclusionsNotice({ ex }: { ex: AvailabilityExclusions }) {
  const total = ex.screens_total
  const also = ex.out_of_inventory.also_conflict ?? []
  return (
    <Alert variant="warning" data-testid="availability-exclusions">
      <AlertTitle>
        Por que {plural(total, 'tela desta seleção ficou', 'telas desta seleção ficaram')} de fora — visível somente para a RD
      </AlertTitle>
      <AlertDescription>
        <ul className="m-0 flex list-disc flex-col gap-2 pl-5">
          {ex.out_of_inventory.by_reason.map((r) => {
            const t = OUT_OF_INVENTORY_TEXT[r.reason] ?? { why: r.reason, next: 'confira o cadastro da tela e do estabelecimento' }
            return (
              <li key={`ooi-${r.reason}`}>
                {plural(r.screens, 'tela está', 'telas estão')} fora do inventário: {t.why}. Próximo passo: {t.next}.
              </li>
            )
          })}
          {also.map((c) => {
            const t = conflictExclusionText(c.reason_code)
            return (
              <li key={`also-${c.reason_code}`}>
                Atenção: mesmo depois de resolver o inventário, {plural(c.screens, 'tela continuaria', 'telas continuariam')} fora por {t.why}.
                Próximo passo: {t.next}.
              </li>
            )
          })}
          {ex.duration.screens > 0 ? (
            <li>
              {plural(ex.duration.screens, 'tela ficou', 'telas ficaram')} fora por duração: o bloco de anúncios da tela é menor que a
              duração do anúncio. Próximo passo: escolha uma duração menor (etapa 2).
            </li>
          ) : null}
          {ex.conflict.by_reason.map((c) => {
            const t = conflictExclusionText(c.reason_code)
            return (
              <li key={`conf-${c.reason_code}`}>
                {plural(c.establishments, 'local excluído', 'locais excluídos')} por {t.why} ({plural(c.screens, 'tela', 'telas')}). Próximo passo:{' '}
                {t.next}.
              </li>
            )
          })}
        </ul>
      </AlertDescription>
    </Alert>
  )
}

export function AvailabilityPanel({
  data,
  loading,
  error,
  showExclusions = false,
}: {
  data?: AvailabilitySummary
  loading?: boolean
  error?: unknown
  /** Equipe RD: exibe o diagnóstico ``exclusions`` (o servidor só o envia à RD; aqui é defesa extra). */
  showExclusions?: boolean
}) {
  if (loading && !data) return <Skeleton className="h-40 w-full" />
  if (error && !data) return <p className="m-0 text-sm text-danger">{describeError(error)}</p>
  if (!data) return <p className="m-0 text-sm text-text-muted">Adicione ao menos um local para ver a disponibilidade.</p>
  const ex = showExclusions && data.exclusions && data.exclusions.screens_total > 0 ? data.exclusions : undefined
  return (
    <div className="flex flex-col gap-4" aria-live="polite" data-testid="availability">
      <dl className="m-0 grid grid-cols-2 gap-4 md:grid-cols-4">
        <Stat term="Telas disponíveis" value={formatNumber(data.screens_available)} />
        <Stat term="Locais (estabelecimentos)" value={formatNumber(data.establishments_count)} />
        <Stat term="Telas com tarja (orientação diferente)" value={formatNumber(data.screens_letterbox)} />
        <Stat term="Fora por duração do bloco" value={formatNumber(data.excluded_by_duration)} />
      </dl>
      {ex ? (
        <ExclusionsNotice ex={ex} />
      ) : data.excluded_by_conflict_establishments > 0 ? (
        <Alert variant="warning">
          <AlertTitle>{formatNumber(data.excluded_by_conflict_establishments)} local(is) excluído(s) por conflito</AlertTitle>
          <AlertDescription>
            Estabelecimentos do mesmo ramo, concorrentes ou com política de conteúdo incompatível com este anúncio não exibem a campanha.
            Os nomes não são exibidos.
          </AlertDescription>
        </Alert>
      ) : null}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <Breakdown title="Por região" column="Região" rows={(data.by_region ?? []).map((r) => [regionLabel(r.region), r.screens])} />
        <Breakdown title="Por UF" column="UF" rows={(data.by_state ?? []).map((r) => [r.state, r.screens])} />
        <Breakdown title="Por cidade" column="Cidade" rows={(data.by_city ?? []).map((r) => [cityLabel(r), r.screens])} />
      </div>
    </div>
  )
}

function Stat({ term, value }: { term: string; value: string }) {
  return (
    <div className="flex flex-col">
      <dt className="text-xs text-text-muted">{term}</dt>
      <dd className="m-0 text-xl font-bold tabular-nums">{value}</dd>
    </div>
  )
}

function Breakdown({ title, column, rows }: { title: string; column: string; rows: Array<[string, number]> }) {
  return (
    <Table>
      <caption className="caption-top pb-2 text-left text-xs font-semibold text-text-muted">{title}</caption>
      <TableHeader>
        <TableRow className="h-11 hover:bg-transparent">
          <TableHead scope="col">{column}</TableHead>
          <TableHead scope="col" numeric>
            Telas
          </TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.length === 0 ? (
          <TableRow>
            <TableCell colSpan={2} className="text-text-muted">
              —
            </TableCell>
          </TableRow>
        ) : (
          rows.slice(0, 30).map(([k, v]) => (
            <TableRow key={k}>
              <TableCell>{k}</TableCell>
              <TableCell numeric className="tabular-nums">
                {formatNumber(v)}
              </TableCell>
            </TableRow>
          ))
        )}
      </TableBody>
    </Table>
  )
}

export function StepTargeting({ campaign, area, onSaved, readOnly }: StepProps) {
  const qc = useQueryClient()
  const [targets, setTargets] = React.useState<Target[]>(campaign?.targets ?? [])
  const [error, setError] = React.useState<string | undefined>()
  const [serverError, setServerError] = React.useState<unknown>(null)
  const [saving, setSaving] = React.useState<'draft' | 'next' | null>(null)
  const debounced = useDebounced(targets, 500)
  const hasInclude = debounced.some((t) => !t.exclude)

  const nodes = useQuery({ queryKey: qk.sub('taxonomy', 'nodes'), queryFn: () => taxonomyApi.nodes(), staleTime: 10 * 60 * 1000 })
  const avail = useQuery({
    queryKey: qk.sub('campaigns', 'availability', campaign?.id ?? '-', campaign?.duration_s ?? 30, debounced),
    queryFn: () => campaignsApi.availability({ campaign_id: campaign!.id, targets: debounced, duration_s: campaign!.duration_s }),
    enabled: !!campaign && hasInclude,
    retry: false,
  })
  const conflicts = useQuery({
    queryKey: qk.sub('campaigns', 'detail', campaign?.id ?? '-', 'conflicts'),
    queryFn: () => campaignsApi.conflicts(campaign!.id),
    enabled: !!campaign && area.isStaff && (campaign?.targets.length ?? 0) > 0,
    retry: false,
  })

  if (!campaign) return <Alert variant="info"><AlertDescription>Salve a identificação primeiro.</AlertDescription></Alert>

  const save = async (advance: boolean) => {
    if (readOnly) return
    if (!targets.some((t) => !t.exclude)) {
      setError('Inclua ao menos um local ou categoria.')
      return
    }
    setError(undefined)
    setServerError(null)
    setSaving(advance ? 'next' : 'draft')
    try {
      await campaignsApi.setTargets(campaign.id, targets)
      // a segmentação incrementa a versão da campanha: recarrega para o próximo PATCH (expected_version)
      const fresh = await campaignsApi.get(campaign.id)
      qc.setQueryData(qk.detail('campaigns', campaign.id), fresh)
      void qc.invalidateQueries({ queryKey: qk.sub('campaigns', 'detail', campaign.id, 'conflicts') })
      toast.success('Segmentação salva.')
      onSaved(fresh, advance)
    } catch (e) {
      const fe = serverFieldErrors(e)
      if (fe.targets) setError(fe.targets)
      else setServerError(e)
    } finally {
      setSaving(null)
    }
  }

  const catNodes = (nodes.data?.items ?? []).filter((n) => n.is_active)

  return (
    <div className="flex flex-col gap-6">
      <TargetingEditor targets={targets} onChange={setTargets} isStaff={area.isStaff} categories={catNodes} disabled={readOnly} error={error} />
      <section className="flex flex-col gap-3" aria-labelledby="availability-title">
        <h3 id="availability-title" className="text-title-card">
          Disponibilidade agora (calculada pelo servidor)
        </h3>
        <AvailabilityPanel
          data={hasInclude ? avail.data : undefined}
          loading={avail.isFetching}
          error={avail.error}
          showExclusions={area.isStaff}
        />
      </section>
      {area.isStaff && conflicts.data?.blocked?.length ? (
        <section className="flex flex-col gap-3" aria-labelledby="conflicts-title">
          <h3 id="conflicts-title" className="text-title-card">
            Conflitos por estabelecimento (segmentação salva) — visível somente para a RD
          </h3>
          <ul className="m-0 flex list-none flex-col gap-3 p-0">
            {conflicts.data.blocked.map((b) => (
              <li key={b.establishment_id} className="flex flex-col gap-2 rounded-lg border border-border p-3">
                <span className="font-semibold">
                  <EstablishmentName id={b.establishment_id} />
                </span>
                <RulePath decision={b} />
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      <ServerError error={serverError} />
      {!readOnly ? <StepActions saving={saving} onSave={(a) => void save(a)} /> : null}
    </div>
  )
}
