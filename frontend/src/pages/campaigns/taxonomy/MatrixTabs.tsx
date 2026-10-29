/**
 * Matriz de conflitos (W-CAMP `/conflicts`): regras explícitas (sujeito × objeto, efeito,
 * descendentes), exceções do estabelecimento, overrides campanha × estabelecimento
 * (justificativa ≥ 20 caracteres; não liberam conteúdo proibido) e simulador
 * "este anúncio pode passar neste estabelecimento?" com o caminho das regras.
 */
import * as React from 'react'
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { FlaskConical, Plus } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Checkbox } from '@/components/ui/checkbox'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Skeleton } from '@/components/ui/skeleton'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { EmptyState, ErrorState, NoPermission } from '@/components/patterns/states'
import { ReasonDialog } from '@/components/campaigns/reason-dialog'
import { RulePath } from '@/components/campaigns/rule-path'
import { EntityPicker, type PickerOption } from '@/components/campaigns/entity-picker'
import { EstablishmentName, EstablishmentTime, OrgName } from '@/components/campaigns/org-name'
import { useTableState } from '@/hooks/useTableState'
import { qk } from '@/lib/queryKeys'
import { formatDateTime } from '@/lib/format'
import { conflictsApi, taxonomyApi } from '../lib/api'
import { searchBrands, searchCampaigns, searchCreatives, searchEstablishments, searchOrganizations, searchProducts } from '../lib/pickers'
import { treeOrder } from '../lib/tree'
import { shortId } from '../lib/format'
import { endOfDayUtc, timeZoneLabel } from '@/lib/dates'
import type { ConflictDecision, ConflictOverride, ConflictRule, RuleObjectType, RuleSubjectType, TreeItem } from '../lib/types'
import { describeError } from '@/pages/campaigns/lib/errorText'
import { TAXONOMY_KIND_LABEL } from '../lib/labels'

const SUBJECT_LABEL: Record<RuleSubjectType, string> = {
  EST_CATEGORY: 'Categoria do estabelecimento',
  EST_SEGMENT: 'Segmento do estabelecimento',
  EST_ORG: 'Rede/empresa do estabelecimento',
  ESTABLISHMENT: 'Estabelecimento específico',
}
const OBJECT_LABEL: Record<RuleObjectType, string> = {
  AD_CATEGORY: 'Categoria do anúncio',
  AD_SEGMENT: 'Segmento do anúncio',
  AD_PRODUCT: 'Produto do anúncio',
  AD_BRAND: 'Marca do anúncio',
  AD_ADVERTISER: 'Anunciante',
}

function useTaxonomyMaps() {
  const nodes = useQuery({ queryKey: qk.sub('taxonomy', 'nodes', { inactive: true }), queryFn: () => taxonomyApi.nodes(true), staleTime: 300000 })
  const segs = useQuery({ queryKey: qk.sub('taxonomy', 'segments', { inactive: true }), queryFn: () => taxonomyApi.segments(true), staleTime: 300000 })
  const nodeList = React.useMemo(() => treeOrder(nodes.data?.items ?? []), [nodes.data])
  const segList = React.useMemo(() => treeOrder(segs.data?.items ?? []), [segs.data])
  const names = React.useMemo(() => Object.fromEntries([...nodeList, ...segList].map((x) => [x.id, x.name])), [nodeList, segList])
  return { nodeList, segList, names }
}

function localSearch(list: TreeItem[]) {
  return async (t: string): Promise<PickerOption[]> =>
    list.filter((x) => !t || x.name.toLowerCase().includes(t.toLowerCase())).map((x) => ({ id: x.id, label: x.name, description: x.kind ? (TAXONOMY_KIND_LABEL[x.kind] ?? x.kind) : undefined }))
}

function RefPicker({
  type,
  value,
  onChange,
  maps,
  label,
}: {
  type: RuleSubjectType | RuleObjectType
  value: PickerOption | null
  onChange: (v: PickerOption | null) => void
  maps: ReturnType<typeof useTaxonomyMaps>
  label: string
}) {
  const search =
    type === 'EST_CATEGORY' || type === 'AD_CATEGORY'
      ? localSearch(maps.nodeList)
      : type === 'EST_SEGMENT' || type === 'AD_SEGMENT'
        ? localSearch(maps.segList)
        : type === 'EST_ORG' || type === 'AD_ADVERTISER'
          ? (t: string) => searchOrganizations(t)
          : type === 'ESTABLISHMENT'
            ? searchEstablishments
            : type === 'AD_PRODUCT'
              ? searchProducts
              : searchBrands
  return <EntityPicker key={type} label={label} domain="taxonomy" value={value} onChange={onChange} search={search} required />
}

function RefName({ type, id, names }: { type: string; id: string; names: Record<string, string> }) {
  if (type === 'EST_ORG' || type === 'AD_ADVERTISER' || type === 'ADVERTISER') return <OrgName id={id} />
  if (type === 'ESTABLISHMENT') return <EstablishmentName id={id} />
  return <span title={id}>{names[id] ?? shortId(id)}</span>
}

// ─── Regras ──────────────────────────────────────────────────────────────────
export function RulesTab() {
  const qc = useQueryClient()
  const maps = useTaxonomyMaps()
  const table = useTableState({ defaultPageSize: 25, prefix: 'r_', filterKeys: ['active'] })
  const params = { page: table.state.page, page_size: table.state.pageSize, active: table.state.filters.active }
  const q = useQuery({ queryKey: qk.list('taxonomy', { rules: params }), queryFn: () => conflictsApi.rules(params), placeholderData: keepPreviousData })
  const [creating, setCreating] = React.useState(false)
  const [toggling, setToggling] = React.useState<ConflictRule | null>(null)
  const cols: DataTableColumn<ConflictRule>[] = [
    {
      id: 'subject',
      header: 'Estabelecimento (sujeito)',
      mobile: 'primary',
      cell: (r) => (
        <span className="flex flex-col">
          <span className="text-xs text-text-muted">{SUBJECT_LABEL[r.subject_type]}</span>
          <RefName type={r.subject_type} id={r.subject_id} names={maps.names} />
        </span>
      ),
    },
    {
      id: 'object',
      header: 'Anúncio (objeto)',
      cell: (r) => (
        <span className="flex flex-col">
          <span className="text-xs text-text-muted">
            {OBJECT_LABEL[r.object_type]}
            {r.include_descendants ? ' + descendentes' : ''}
          </span>
          <RefName type={r.object_type} id={r.object_id} names={maps.names} />
        </span>
      ),
    },
    { id: 'effect', header: 'Efeito', mobile: 'badge', cell: (r) => (r.effect === 'BLOCK' ? <Badge tone="danger">Bloqueia</Badge> : <Badge tone="success">Libera</Badge>) },
    { id: 'reason', header: 'Justificativa', cell: (r) => r.reason },
    {
      id: 'validity',
      header: 'Vigência',
      cell: (r) => {
        // regra de um estabelecimento específico: vigência no fuso dele (como foi gravada)
        const at = (v: string) => (r.subject_type === 'ESTABLISHMENT' ? <EstablishmentTime establishmentId={r.subject_id} value={v} /> : formatDateTime(v))
        return (
          <>
            {r.valid_from ? at(r.valid_from) : 'sem início'} → {r.valid_until ? at(r.valid_until) : 'sem fim'}
          </>
        )
      },
    },
    { id: 'active', header: 'Situação', cell: (r) => (r.active ? <Badge tone="success">Ativa (v{r.version})</Badge> : <Badge>Inativa</Badge>) },
  ]
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-title-section">Regras explícitas da matriz</h2>
        <Button onClick={() => setCreating(true)}>
          <Plus aria-hidden="true" />
          Nova regra
        </Button>
      </div>
      <p className="m-0 text-sm text-text-muted">A regra mais específica vence (objeto, depois sujeito); empate resulta em bloqueio.</p>
      <DataTable
        caption="Regras de conflito"
        columns={cols}
        data={q.data}
        getRowId={(r) => r.id}
        controller={table}
        isLoading={q.isLoading}
        isFetching={q.isFetching}
        error={q.error}
        onRetry={() => void q.refetch()}
        filtersActive={!!table.state.filters.active}
        empty={{ title: 'Nenhuma regra explícita', description: 'Sem regras, valem mesmo ramo, segmento, concorrência e a política de conteúdo.' }}
        rowActions={(r) => (
          <Button variant="ghost" onClick={() => setToggling(r)}>
            {r.active ? 'Desativar' : 'Reativar'}
          </Button>
        )}
      />
      <RuleDialog
        open={creating}
        onOpenChange={setCreating}
        maps={maps}
        onSaved={() => void qc.invalidateQueries({ queryKey: qk.root('taxonomy') })}
      />
      <ReasonDialog
        open={!!toggling}
        onOpenChange={(o) => !o && setToggling(null)}
        title={toggling?.active ? 'Desativar a regra?' : 'Reativar a regra?'}
        minLength={5}
        confirmLabel={toggling?.active ? 'Desativar' : 'Reativar'}
        onConfirm={async (reason) => {
          await conflictsApi.patchRule(toggling!.id, { active: !toggling!.active, reason })
          toast.success('Regra atualizada.')
          void qc.invalidateQueries({ queryKey: qk.root('taxonomy') })
        }}
      />
    </div>
  )
}

function RuleDialog({ open, onOpenChange, maps, onSaved }: { open: boolean; onOpenChange: (o: boolean) => void; maps: ReturnType<typeof useTaxonomyMaps>; onSaved: () => void }) {
  const [subjectType, setSubjectType] = React.useState<RuleSubjectType>('EST_CATEGORY')
  const [objectType, setObjectType] = React.useState<RuleObjectType>('AD_CATEGORY')
  const [subject, setSubject] = React.useState<PickerOption | null>(null)
  const [object, setObject] = React.useState<PickerOption | null>(null)
  const [effect, setEffect] = React.useState<'BLOCK' | 'ALLOW'>('BLOCK')
  const [desc, setDesc] = React.useState(true)
  const [reason, setReason] = React.useState('')
  const [until, setUntil] = React.useState('')
  // regra cujo sujeito é um estabelecimento: a data vale no fuso dele; demais regras, no fuso padrão
  const ruleTz = subjectType === 'ESTABLISHMENT' ? (subject?.timezone ?? undefined) : undefined
  const [errors, setErrors] = React.useState<Record<string, string>>({})
  const [error, setError] = React.useState<unknown>(null)
  const [pending, setPending] = React.useState(false)
  React.useEffect(() => {
    if (open) {
      setSubject(null)
      setObject(null)
      setReason('')
      setUntil('')
      setErrors({})
      setError(null)
    }
  }, [open])
  const save = async () => {
    const e: Record<string, string> = {}
    if (!subject) e.subject = 'Escolha o sujeito.'
    if (!object) e.object = 'Escolha o objeto.'
    if (reason.trim().length < 5) e.reason = 'Mínimo de 5 caracteres.'
    setErrors(e)
    if (Object.keys(e).length) return
    setPending(true)
    setError(null)
    try {
      await conflictsApi.createRule({
        subject_type: subjectType,
        subject_id: subject!.id,
        object_type: objectType,
        object_id: object!.id,
        include_descendants: desc,
        effect,
        reason: reason.trim(),
        valid_until: until ? endOfDayUtc(until, ruleTz) : null,
      })
      toast.success('Regra criada.')
      onSaved()
      onOpenChange(false)
    } catch (err) {
      setError(err)
    } finally {
      setPending(false)
    }
  }
  return (
    <Dialog open={open} onOpenChange={(o) => !pending && onOpenChange(o)}>
      <DialogContent size="md" className="max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Nova regra da matriz</DialogTitle>
          <DialogDescription>Quando o estabelecimento (sujeito) receber anúncio do objeto, aplicar o efeito.</DialogDescription>
        </DialogHeader>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Field>
            <FieldLabel>Tipo do sujeito</FieldLabel>
            <Select value={subjectType} onValueChange={(v) => { setSubjectType(v as RuleSubjectType); setSubject(null) }}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  {(Object.keys(SUBJECT_LABEL) as RuleSubjectType[]).map((k) => (
                    <SelectItem key={k} value={k}>
                      {SUBJECT_LABEL[k]}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
          </Field>
          <Field>
            <FieldLabel>Tipo do objeto</FieldLabel>
            <Select value={objectType} onValueChange={(v) => { setObjectType(v as RuleObjectType); setObject(null) }}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  {(Object.keys(OBJECT_LABEL) as RuleObjectType[]).map((k) => (
                    <SelectItem key={k} value={k}>
                      {OBJECT_LABEL[k]}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
          </Field>
          <div>
            <RefPicker type={subjectType} value={subject} onChange={setSubject} maps={maps} label={SUBJECT_LABEL[subjectType]} />
            <FieldError>{errors.subject}</FieldError>
          </div>
          <div>
            <RefPicker type={objectType} value={object} onChange={setObject} maps={maps} label={OBJECT_LABEL[objectType]} />
            <FieldError>{errors.object}</FieldError>
          </div>
        </div>
        <div className="flex flex-col gap-2">
          <span id="rule-effect" className="text-sm font-semibold">
            Efeito
          </span>
          <ToggleGroup type="single" value={effect} onValueChange={(v) => v && setEffect(v as 'BLOCK' | 'ALLOW')} aria-labelledby="rule-effect">
            <ToggleGroupItem value="BLOCK">Bloquear</ToggleGroupItem>
            <ToggleGroupItem value="ALLOW">Liberar</ToggleGroupItem>
          </ToggleGroup>
        </div>
        <Field orientation="horizontal" className="min-h-11">
          <Checkbox checked={desc} onCheckedChange={(c) => setDesc(c === true)} />
          <FieldLabel className="font-normal">Incluir descendentes do objeto (subcategorias/subsegmentos)</FieldLabel>
        </Field>
        <Field>
          <FieldLabel>Válida até (opcional)</FieldLabel>
          <Input type="date" value={until} onChange={(e) => setUntil(e.target.value)} />
          <FieldDescription>Vale até 23:59:59 no {timeZoneLabel(ruleTz)}.</FieldDescription>
        </Field>
        <Field invalid={!!errors.reason}>
          <FieldLabel required>Justificativa</FieldLabel>
          <Textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={3} />
          <FieldError>{errors.reason}</FieldError>
        </Field>
        {effect === 'ALLOW' ? (
          <p className="m-0 text-xs text-text-muted">Uma regra de liberação nunca libera conteúdo proibido pela política universal.</p>
        ) : null}
        {error ? (
          <Alert variant="danger">
            <AlertDescription>{describeError(error)}</AlertDescription>
          </Alert>
        ) : null}
        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)} disabled={pending}>
            Cancelar
          </Button>
          <Button onClick={() => void save()} loading={pending}>
            Criar regra
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ─── Exceções do estabelecimento ─────────────────────────────────────────────
export function ExceptionsTab() {
  const qc = useQueryClient()
  const [est, setEst] = React.useState<PickerOption | null>(null)
  const [objType, setObjType] = React.useState<'ADVERTISER' | 'BRAND' | 'CAMPAIGN'>('ADVERTISER')
  const [obj, setObj] = React.useState<PickerOption | null>(null)
  const [creating, setCreating] = React.useState(false)
  const [revoking, setRevoking] = React.useState<string | null>(null)
  const q = useQuery({ queryKey: qk.sub('taxonomy', 'exceptions'), queryFn: () => conflictsApi.exceptions() })
  const refresh = () => void qc.invalidateQueries({ queryKey: qk.sub('taxonomy', 'exceptions') })
  const searchObj = objType === 'ADVERTISER' ? (t: string) => searchOrganizations(t) : objType === 'BRAND' ? searchBrands : searchCampaigns
  return (
    <div className="flex flex-col gap-4">
      <h2 className="text-title-section">Exceções do estabelecimento</h2>
      <p className="m-0 text-sm text-text-muted">
        Libera um anunciante, marca ou campanha específicos num estabelecimento que normalmente os bloquearia. Somente a RD cria; o estabelecimento solicita por
        chamado. Não libera conteúdo proibido.
      </p>
      <div className="grid grid-cols-1 gap-3 rounded-lg border border-border p-4 md:grid-cols-3 md:items-start">
        <EntityPicker label="Estabelecimento" domain="establishments" value={est} onChange={setEst} search={searchEstablishments} />
        <Field>
          <FieldLabel>Liberar</FieldLabel>
          <Select value={objType} onValueChange={(v) => { setObjType(v as typeof objType); setObj(null) }}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                <SelectItem value="ADVERTISER">Anunciante</SelectItem>
                <SelectItem value="BRAND">Marca</SelectItem>
                <SelectItem value="CAMPAIGN">Campanha</SelectItem>
              </SelectGroup>
            </SelectContent>
          </Select>
        </Field>
        <EntityPicker key={objType} label="Objeto" domain="taxonomy" value={obj} onChange={setObj} search={searchObj} />
        <Button className="w-fit" onClick={() => setCreating(true)} disabled={!est || !obj}>
          Criar exceção
        </Button>
      </div>
      {q.isLoading ? (
        <Skeleton className="h-24 w-full" />
      ) : q.error ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} size="compact" />
      ) : !q.data?.items.length ? (
        <EmptyState size="compact" headingLevel="h3" title="Nenhuma exceção cadastrada" />
      ) : (
        <Table>
          <caption className="sr-only">Exceções</caption>
          <TableHeader>
            <TableRow className="h-11 hover:bg-transparent">
              <TableHead scope="col">Estabelecimento</TableHead>
              <TableHead scope="col">Liberado</TableHead>
              <TableHead scope="col">Justificativa</TableHead>
              <TableHead scope="col">Vigência</TableHead>
              <TableHead scope="col" className="text-right">
                Ações
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {q.data.items.map((x) => (
              <TableRow key={x.id}>
                <TableCell>
                  <EstablishmentName id={x.establishment_id} />
                </TableCell>
                <TableCell>
                  {x.object_type === 'ADVERTISER' ? <OrgName id={x.object_id} /> : `${x.object_type === 'BRAND' ? 'Marca' : 'Campanha'} ${shortId(x.object_id)}`}
                </TableCell>
                <TableCell>{x.reason}</TableCell>
                <TableCell>
                  {x.revoked_at ? (
                    <Badge>Revogada {formatDateTime(x.revoked_at)}</Badge>
                  ) : (
                    <>
                      {x.valid_from ? <EstablishmentTime establishmentId={x.establishment_id} value={x.valid_from} /> : 'sem início'} →{' '}
                      {x.valid_until ? <EstablishmentTime establishmentId={x.establishment_id} value={x.valid_until} /> : 'sem fim'}
                    </>
                  )}
                </TableCell>
                <TableCell className="text-right">
                  {!x.revoked_at ? (
                    <Button variant="ghost" onClick={() => setRevoking(x.id)}>
                      Revogar
                    </Button>
                  ) : null}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      <ReasonDialog
        open={creating}
        onOpenChange={setCreating}
        title={`Liberar ${obj?.label ?? ''} em ${est?.label ?? ''}?`}
        minLength={10}
        reasonLabel="Justificativa"
        confirmLabel="Criar exceção"
        onConfirm={async (reason) => {
          await conflictsApi.createException({ establishment_id: est!.id, object_type: objType, object_id: obj!.id, reason })
          toast.success('Exceção criada.')
          setObj(null)
          refresh()
        }}
      />
      <ReasonDialog
        open={!!revoking}
        onOpenChange={(o) => !o && setRevoking(null)}
        title="Revogar a exceção?"
        minLength={5}
        destructive
        confirmLabel="Revogar"
        onConfirm={async (reason) => {
          await conflictsApi.revokeException(revoking!, reason)
          toast.success('Exceção revogada.')
          refresh()
        }}
      />
    </div>
  )
}

// ─── Overrides administrativos ───────────────────────────────────────────────
export function OverridesTab({ canOverride }: { canOverride: boolean }) {
  const qc = useQueryClient()
  const [camp, setCamp] = React.useState<PickerOption | null>(null)
  const [est, setEst] = React.useState<PickerOption | null>(null)
  const [justification, setJustification] = React.useState('')
  const [expires, setExpires] = React.useState('')
  const [touched, setTouched] = React.useState(false)
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<unknown>(null)
  const [last, setLast] = React.useState<ConflictOverride | null>(null)
  const [revoking, setRevoking] = React.useState<string | null>(null)
  const q = useQuery({ queryKey: qk.sub('taxonomy', 'overrides'), queryFn: () => conflictsApi.overrides(), enabled: canOverride })
  if (!canOverride) return <NoPermission permission="conflicts.override" />
  const short = justification.trim().length < 20
  const create = async () => {
    setTouched(true)
    if (!camp || !est || short) return
    setPending(true)
    setError(null)
    try {
      const o = await conflictsApi.createOverride({
        campaign_id: camp.id,
        establishment_id: est.id,
        justification: justification.trim(),
        expires_at: expires ? endOfDayUtc(expires, est.timezone ?? undefined) : null,
      })
      setLast(o)
      setJustification('')
      setTouched(false)
      toast.success('Override registrado.')
      void qc.invalidateQueries({ queryKey: qk.sub('taxonomy', 'overrides') })
    } catch (e) {
      setError(e)
    } finally {
      setPending(false)
    }
  }
  return (
    <div className="flex flex-col gap-4">
      <h2 className="text-title-section">Overrides (campanha × estabelecimento)</h2>
      <Alert variant="warning">
        <AlertDescription>
          Use somente com justificativa forte (mínimo de 20 caracteres). Fica na auditoria com seu usuário e IP. Não libera conteúdo proibido pela política
          universal.
        </AlertDescription>
      </Alert>
      <div className="grid grid-cols-1 gap-3 rounded-lg border border-border p-4 md:grid-cols-2">
        <EntityPicker label="Campanha" domain="campaigns" value={camp} onChange={setCamp} search={searchCampaigns} required />
        <EntityPicker label="Estabelecimento" domain="establishments" value={est} onChange={setEst} search={searchEstablishments} required />
        <Field invalid={touched && short} className="md:col-span-2">
          <FieldLabel required>Justificativa</FieldLabel>
          <Textarea value={justification} onChange={(e) => setJustification(e.target.value)} rows={3} />
          <FieldDescription>{justification.trim().length}/20 caracteres no mínimo.</FieldDescription>
          <FieldError>{touched && short ? 'A justificativa precisa ter ao menos 20 caracteres.' : null}</FieldError>
        </Field>
        <Field>
          <FieldLabel>Expira em (opcional)</FieldLabel>
          <Input type="date" value={expires} onChange={(e) => setExpires(e.target.value)} />
          <FieldDescription>Expira às 23:59:59 no {timeZoneLabel(est?.timezone)}.</FieldDescription>
        </Field>
        <div className="flex items-end">
          <Button onClick={() => void create()} loading={pending} disabled={!camp || !est}>
            Registrar override
          </Button>
        </div>
      </div>
      {error ? (
        <Alert variant="danger">
          <AlertDescription>{describeError(error)}</AlertDescription>
        </Alert>
      ) : null}
      {last ? (
        <div className="flex flex-col gap-3 rounded-lg border border-border p-4" role="status">
          {last.warning ? (
            <Alert variant="danger">
              <AlertTitle>Override sem efeito</AlertTitle>
              <AlertDescription>{last.warning}</AlertDescription>
            </Alert>
          ) : null}
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {last.decision_before ? (
              <div>
                <p className="m-0 mb-2 text-xs font-semibold text-text-muted">Antes</p>
                <RulePath decision={last.decision_before} />
              </div>
            ) : null}
            {last.decision_after ? (
              <div>
                <p className="m-0 mb-2 text-xs font-semibold text-text-muted">Depois</p>
                <RulePath decision={last.decision_after} />
              </div>
            ) : null}
          </div>
        </div>
      ) : null}
      {q.data?.items.length ? (
        <Table>
          <caption className="sr-only">Overrides</caption>
          <TableHeader>
            <TableRow className="h-11 hover:bg-transparent">
              <TableHead scope="col">Campanha</TableHead>
              <TableHead scope="col">Estabelecimento</TableHead>
              <TableHead scope="col">Justificativa</TableHead>
              <TableHead scope="col">Aprovado em</TableHead>
              <TableHead scope="col">Situação</TableHead>
              <TableHead scope="col" className="text-right">
                Ações
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {q.data.items.map((o) => (
              <TableRow key={o.id}>
                <TableCell>{shortId(o.campaign_id)}</TableCell>
                <TableCell>
                  <EstablishmentName id={o.establishment_id} />
                </TableCell>
                <TableCell>{o.justification}</TableCell>
                <TableCell>{formatDateTime(o.approved_at)}</TableCell>
                <TableCell>
                  {o.revoked_at ? (
                    <Badge>Revogado</Badge>
                  ) : o.expires_at ? (
                    <>
                      Ativo até <EstablishmentTime establishmentId={o.establishment_id} value={o.expires_at} />
                    </>
                  ) : (
                    <Badge tone="warning">Ativo</Badge>
                  )}
                </TableCell>
                <TableCell className="text-right">
                  {!o.revoked_at ? (
                    <Button variant="ghost" onClick={() => setRevoking(o.id)}>
                      Revogar
                    </Button>
                  ) : null}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      ) : q.isLoading ? (
        <Skeleton className="h-24 w-full" />
      ) : null}
      <ReasonDialog
        open={!!revoking}
        onOpenChange={(o) => !o && setRevoking(null)}
        title="Revogar o override?"
        minLength={5}
        destructive
        confirmLabel="Revogar"
        onConfirm={async (reason) => {
          await conflictsApi.revokeOverride(revoking!, reason)
          toast.success('Override revogado.')
          void qc.invalidateQueries({ queryKey: qk.sub('taxonomy', 'overrides') })
        }}
      />
    </div>
  )
}

// ─── Simulador ───────────────────────────────────────────────────────────────
export function SimulatorTab() {
  const [mode, setMode] = React.useState<'campaign' | 'creative' | 'ad_vs_ad'>('campaign')
  const [subject, setSubject] = React.useState<PickerOption | null>(null)
  const [other, setOther] = React.useState<PickerOption | null>(null)
  const [est, setEst] = React.useState<PickerOption | null>(null)
  const [result, setResult] = React.useState<(ConflictDecision & { mode: string; competitors?: boolean }) | null>(null)
  const [error, setError] = React.useState<unknown>(null)
  const [pending, setPending] = React.useState(false)
  const ready = !!subject && (mode === 'ad_vs_ad' ? !!other : !!est)
  const run = async () => {
    if (!ready) return
    setPending(true)
    setError(null)
    setResult(null)
    try {
      const body =
        mode === 'ad_vs_ad'
          ? { creative_id: subject!.id, other_creative_id: other!.id }
          : mode === 'campaign'
            ? { campaign_id: subject!.id, establishment_id: est!.id }
            : { creative_id: subject!.id, establishment_id: est!.id }
      setResult(await conflictsApi.evaluate(body))
    } catch (e) {
      setError(e)
    } finally {
      setPending(false)
    }
  }
  return (
    <div className="flex flex-col gap-4">
      <h2 className="text-title-section">Simulador: este anúncio pode passar neste estabelecimento?</h2>
      <ToggleGroup
        type="single"
        value={mode}
        onValueChange={(v) => {
          if (!v) return
          setMode(v as typeof mode)
          setSubject(null)
          setOther(null)
          setResult(null)
        }}
        aria-label="Tipo de simulação"
      >
        <ToggleGroupItem value="campaign">Campanha × estabelecimento</ToggleGroupItem>
        <ToggleGroupItem value="creative">Anúncio × estabelecimento</ToggleGroupItem>
        <ToggleGroupItem value="ad_vs_ad">Anúncio × anúncio (concorrência)</ToggleGroupItem>
      </ToggleGroup>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <EntityPicker
          key={`s-${mode}`}
          label={mode === 'campaign' ? 'Campanha' : 'Anúncio (código RD)'}
          domain={mode === 'campaign' ? 'campaigns' : 'creatives'}
          value={subject}
          onChange={setSubject}
          search={mode === 'campaign' ? searchCampaigns : searchCreatives}
        />
        {mode === 'ad_vs_ad' ? (
          <EntityPicker label="Outro anúncio" domain="creatives" value={other} onChange={setOther} search={searchCreatives} />
        ) : (
          <EntityPicker label="Estabelecimento" domain="establishments" value={est} onChange={setEst} search={searchEstablishments} />
        )}
      </div>
      <Button className="w-fit" onClick={() => void run()} loading={pending} disabled={!ready}>
        <FlaskConical aria-hidden="true" />
        Simular
      </Button>
      <p className="m-0 text-xs text-text-muted">A simulação não grava decisão nem altera a campanha.</p>
      {error ? (
        <Alert variant="danger">
          <AlertDescription>{describeError(error)}</AlertDescription>
        </Alert>
      ) : null}
      {result ? (
        <div className="rounded-lg border border-border p-4" role="status">
          {result.mode === 'AD_VS_AD' ? (
            <p className="m-0 mb-2 text-sm font-semibold">{result.competitors ? 'São concorrentes: nunca no mesmo bloco.' : 'Não são concorrentes.'}</p>
          ) : null}
          <RulePath decision={result} />
        </div>
      ) : null}
    </div>
  )
}
