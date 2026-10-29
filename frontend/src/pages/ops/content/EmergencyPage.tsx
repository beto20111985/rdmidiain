/**
 * Comunicado de emergência (RD, campaigns.kill global): mensagem urgente para todas as telas,
 * telas, estabelecimentos, UFs ou cidades, com janela (início/fim) e prioridade máxima na grade.
 * Confirmação forte (texto digitado) e motivo auditado; cancelamento com motivo.
 * Backend: GET/POST /scheduler/emergencies, POST /scheduler/emergencies/{id}/cancel (W-INV).
 * Horários digitados no fuso America/Sao_Paulo e enviados em UTC.
 */
import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Siren, X } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Checkbox } from '@/components/ui/checkbox'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { EmptyState, ErrorState, NoPermission } from '@/components/patterns/states'
import { OptionSelect } from '@/components/ops/filter-select'
import { CreativePicker, EstablishmentPicker, ScreenPicker } from '@/components/ops/pickers'
import { EstablishmentName, ScreenCode } from '@/components/ops/entity-name'
import { ReasonDialog } from '@/components/ops/reason-dialog'
import { utcToZonedLocalInput, zonedLocalToUtcIso } from '@/lib/dates'
import { errorMessage, isApiError } from '@/lib/errors'
import { qk } from '@/lib/queryKeys'
import { DEFAULT_TIMEZONE, formatDateTime } from '@/lib/format'
import { scheduleApi } from '@/pages/ops/api/client'
import { EMERGENCY_TARGET_LABEL, UFS, labelOf } from '@/pages/ops/api/labels'
import type { Emergency } from '@/pages/ops/api/types'

type Target = Emergency['target_kind']
const TZ = DEFAULT_TIMEZONE
const CONFIRM_WORD = 'EMERGENCIA'

export default function EmergencyPage() {
  const qc = useQueryClient()
  const [activeOnly, setActiveOnly] = React.useState(true)
  const list = useQuery({
    queryKey: qk.sub('schedule', 'emergencies', activeOnly ? 'active' : 'all'),
    queryFn: () => scheduleApi.emergencies(activeOnly),
  })
  const [cancelling, setCancelling] = React.useState<Emergency | null>(null)
  const cancel = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) => scheduleApi.cancelEmergency(id, reason),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['schedule'] })
      toast.success('Comunicado cancelado. As telas voltam à programação normal na próxima sincronização.')
    },
  })

  if (isApiError(list.error) && list.error.isForbidden) return <NoPermission permission="campaigns.kill" />

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Comunicado de emergência"
        description="Mensagem urgente com prioridade máxima na grade das telas escolhidas, dentro de uma janela de tempo."
        breadcrumbs={[{ label: 'Conteúdo interno', to: '/conteudo-interno' }, { label: 'Emergência' }]}
      />
      <EmergencyForm />
      <Card>
        <CardHeader>
          <CardTitle>Comunicados</CardTitle>
          <Field orientation="horizontal" className="min-h-11 w-auto">
            <Switch checked={activeOnly} onCheckedChange={setActiveOnly} />
            <FieldLabel>Somente ativos</FieldLabel>
          </Field>
        </CardHeader>
        {list.isLoading ? (
          <Skeleton className="h-32 w-full" />
        ) : list.error ? (
          <ErrorState error={list.error} onRetry={() => void list.refetch()} size="compact" />
        ) : !list.data?.items.length ? (
          <EmptyState icon={Siren} size="compact" headingLevel="h3" title={activeOnly ? 'Nenhum comunicado ativo.' : 'Nenhum comunicado registrado.'} />
        ) : (
          <ul className="m-0 flex list-none flex-col divide-y divide-border p-0">
            {list.data.items.map((e) => (
              <li key={e.id} className="flex flex-col gap-2 py-4 sm:flex-row sm:items-start sm:justify-between">
                <div className="flex min-w-0 flex-col gap-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-semibold text-text">{e.title}</span>
                    <Badge tone={e.status === 'ACTIVE' ? 'danger' : 'neutral'}>{e.status === 'ACTIVE' ? 'Ativo' : 'Cancelado'}</Badge>
                  </div>
                  <span className="text-sm text-text-muted">
                    {formatDateTime(e.starts_at, TZ)} → {formatDateTime(e.ends_at, TZ)} (horário de Brasília) · {labelOf(EMERGENCY_TARGET_LABEL, e.target_kind)}
                    {e.target_kind !== 'ALL' ? ` (${e.target_ids.length})` : ''}
                  </span>
                  <span className="text-xs text-text-muted">Motivo: {e.reason}</span>
                  {e.cancelled_at ? (
                    <span className="text-xs text-text-muted">
                      Cancelado em {formatDateTime(e.cancelled_at, TZ)}: {e.cancel_reason}
                    </span>
                  ) : null}
                </div>
                {e.status === 'ACTIVE' ? (
                  <Button variant="destructive" onClick={() => setCancelling(e)}>
                    Cancelar comunicado
                  </Button>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Card>
      <ReasonDialog
        open={!!cancelling}
        onOpenChange={(o) => !o && setCancelling(null)}
        title={`Cancelar “${cancelling?.title ?? ''}”?`}
        consequences={['As telas do alvo voltam à programação normal na próxima sincronização.']}
        confirmLabel="Cancelar comunicado"
        cancelLabel="Voltar"
        destructive
        onConfirm={(reason) => cancel.mutateAsync({ id: cancelling!.id, reason })}
      />
    </div>
  )
}

function EmergencyForm() {
  const qc = useQueryClient()
  const now = Date.now()
  const [title, setTitle] = React.useState('')
  const [creativeId, setCreativeId] = React.useState<string | null>(null)
  const [startsAt, setStartsAt] = React.useState(() => utcToZonedLocalInput(now, TZ))
  const [endsAt, setEndsAt] = React.useState(() => utcToZonedLocalInput(now + 60 * 60 * 1000, TZ))
  const [target, setTarget] = React.useState<Target>('ALL')
  const [ids, setIds] = React.useState<string[]>([])
  const [pick, setPick] = React.useState<string | null>(null)
  const [cities, setCities] = React.useState('')
  const [touched, setTouched] = React.useState(false)
  const [confirmOpen, setConfirmOpen] = React.useState(false)

  const startIso = zonedLocalToUtcIso(startsAt, TZ)
  const endIso = zonedLocalToUtcIso(endsAt, TZ)
  const cityKeys = cities
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean)
  const targetIds = target === 'CITIES' ? cityKeys : ids
  const errs = {
    title: title.trim().length < 3 ? 'Informe um título (mínimo 3 caracteres).' : null,
    creative: !creativeId ? 'Selecione a peça a exibir.' : null,
    window:
      !startIso || !endIso
        ? 'Informe início e fim.'
        : new Date(endIso) <= new Date(startIso)
          ? 'O fim deve ser depois do início.'
          : new Date(endIso).getTime() <= Date.now()
            ? 'A janela já terminou.'
            : null,
    targets:
      target !== 'ALL' && !targetIds.length
        ? 'Escolha pelo menos um alvo.'
        : target === 'CITIES' && cityKeys.some((c) => !/^[A-Za-z]{2}:.{2,}$/.test(c))
          ? 'Use uma cidade por linha no formato UF:Nome da cidade (ex.: SP:Campinas).'
          : null,
  }
  const valid = !Object.values(errs).some(Boolean)

  const m = useMutation({
    mutationFn: (reason: string) =>
      scheduleApi.createEmergency({
        title: title.trim(),
        creative_id: creativeId,
        starts_at: startIso,
        ends_at: endIso,
        target_kind: target,
        target_ids: target === 'ALL' ? [] : targetIds,
        reason,
      }),
    onSuccess: (e) => {
      void qc.invalidateQueries({ queryKey: ['schedule'] })
      toast.success(`Comunicado publicado em ${e.screens ?? 0} tela(s). As telas online recebem a mudança imediatamente.`)
      setTitle('')
      setCreativeId(null)
      setIds([])
      setCities('')
      setTarget('ALL')
      setTouched(false)
    },
  })

  const addId = () => {
    if (pick && !ids.includes(pick)) setIds((p) => [...p, pick])
    setPick(null)
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Novo comunicado</CardTitle>
          <CardDescription>Use somente para avisos urgentes (segurança, evacuação, fechamento). Tudo é auditado.</CardDescription>
        </div>
      </CardHeader>
      <form
        noValidate
        className="flex flex-col gap-5"
        onSubmit={(e) => {
          e.preventDefault()
          setTouched(true)
          if (valid) setConfirmOpen(true)
        }}
      >
        <Field invalid={touched && !!errs.title} required>
          <FieldLabel>Título</FieldLabel>
          <Input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={200} />
          <FieldError>{touched ? errs.title : null}</FieldError>
        </Field>
        <CreativePicker
          kinds={['INSTITUTIONAL', 'FALLBACK', 'INTERNAL', 'EXTERNAL']}
          value={creativeId}
          onChange={setCreativeId}
          required
          legend="Peça a exibir (aprovada, com mídia pronta)"
          error={touched ? errs.creative : null}
        />
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field invalid={touched && !!errs.window} required>
            <FieldLabel>Início (horário de Brasília)</FieldLabel>
            <Input type="datetime-local" value={startsAt} onChange={(e) => setStartsAt(e.target.value)} />
          </Field>
          <Field invalid={touched && !!errs.window} required>
            <FieldLabel>Fim (horário de Brasília)</FieldLabel>
            <Input type="datetime-local" value={endsAt} onChange={(e) => setEndsAt(e.target.value)} />
            <FieldError>{touched ? errs.window : null}</FieldError>
          </Field>
        </div>
        <Field required>
          <FieldLabel>Alvo</FieldLabel>
          <OptionSelect
            value={target}
            onChange={(v) => {
              setTarget((v ?? 'ALL') as Target)
              setIds([])
              setPick(null)
            }}
            options={Object.entries(EMERGENCY_TARGET_LABEL).map(([value, label]) => ({ value, label }))}
          />
        </Field>
        {target === 'SCREENS' || target === 'ESTABLISHMENTS' ? (
          <div className="flex flex-col gap-3">
            {target === 'SCREENS' ? (
              <ScreenPicker value={pick} onChange={setPick} legend="Adicionar tela" />
            ) : (
              <EstablishmentPicker value={pick} onChange={setPick} legend="Adicionar estabelecimento" onlyActive />
            )}
            <Button type="button" variant="secondary" className="w-fit" onClick={addId} disabled={!pick}>
              Adicionar ao alvo
            </Button>
            <ul className="m-0 flex list-none flex-wrap gap-2 p-0" aria-label="Alvos selecionados">
              {ids.map((id) => (
                <li key={id}>
                  <Badge tone="outline">
                    {target === 'SCREENS' ? <ScreenCode id={id} link={false} /> : <EstablishmentName id={id} link={false} />}
                    <button
                      type="button"
                      className="ml-1 inline-flex size-6 items-center justify-center rounded text-text-muted hover:text-text"
                      aria-label="Remover alvo"
                      onClick={() => setIds((p) => p.filter((x) => x !== id))}
                    >
                      <X aria-hidden="true" className="size-3.5" />
                    </button>
                  </Badge>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        {target === 'STATES' ? (
          <fieldset className="m-0 grid grid-cols-2 gap-0 border-0 p-0 sm:grid-cols-4 xl:grid-cols-7">
            <legend className="mb-2 text-sm font-semibold text-text">Estados</legend>
            {UFS.map((u) => (
              <Field key={u.uf} orientation="horizontal" className="min-h-11">
                <Checkbox
                  checked={ids.includes(u.uf)}
                  onCheckedChange={(c) => setIds((p) => (c ? [...p, u.uf] : p.filter((x) => x !== u.uf)))}
                />
                <FieldLabel className="font-normal">{u.uf}</FieldLabel>
              </Field>
            ))}
          </fieldset>
        ) : null}
        {target === 'CITIES' ? (
          <Field invalid={touched && !!errs.targets} required>
            <FieldLabel>Cidades (uma por linha)</FieldLabel>
            <Textarea value={cities} onChange={(e) => setCities(e.target.value)} rows={4} placeholder={'SP:São Paulo\nRJ:Niterói'} />
            <FieldDescription>Formato UF:Nome da cidade.</FieldDescription>
          </Field>
        ) : null}
        {touched && errs.targets ? <p className="m-0 text-xs text-danger">{errs.targets}</p> : null}
        {m.error ? (
          <Alert variant="danger">
            <AlertDescription className="text-text">{errorMessage(m.error)}</AlertDescription>
          </Alert>
        ) : null}
        <Alert variant="warning">
          <AlertTitle>Prioridade máxima</AlertTitle>
          <AlertDescription>Durante a janela, o comunicado substitui anúncios e conteúdo interno nas telas do alvo.</AlertDescription>
        </Alert>
        <Button type="submit" variant="destructive" className="w-fit">
          <Siren aria-hidden="true" />
          Revisar e publicar
        </Button>
      </form>
      <ReasonDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title={`Publicar o comunicado “${title.trim()}”?`}
        description={`${labelOf(EMERGENCY_TARGET_LABEL, target)}${target !== 'ALL' ? ` (${targetIds.length})` : ''} · ${startIso ? formatDateTime(startIso, TZ) : '—'} → ${endIso ? formatDateTime(endIso, TZ) : '—'}`}
        consequences={[
          'O comunicado interrompe anúncios e conteúdo interno nas telas do alvo durante a janela.',
          'Exibições de anúncios previstas para a janela podem não acontecer nas telas do alvo.',
          'A publicação é registrada na auditoria e notifica a equipe.',
        ]}
        confirmLabel="Publicar comunicado"
        destructive
        requireText={CONFIRM_WORD}
        onConfirm={(reason) => m.mutateAsync(reason)}
      />
    </Card>
  )
}
