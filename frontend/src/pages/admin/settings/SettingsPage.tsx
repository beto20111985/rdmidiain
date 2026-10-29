/**
 * Configurações do sistema (`settings.view|manage`) — GET /settings (registry tipado do
 * backend) e PUT /settings/{key} com versão otimista e motivo auditado.
 * Chaves FINANCEIRAS (modelo/pool de comissão, margem mínima, cooldown PIX, reembolso…)
 * não mudam direto: o backend responde 409 SETTING_REQUIRES_DUAL_APPROVAL e a alteração
 * vira um pedido de aprovação (POST /financial/approvals, ação SETTING_CHANGE): na DUPLA é aprovado
 * pelo outro Master em Financeiro → Aprovações; na ÚNICA (AÇÃO 003) o próprio Master autoriza aqui mesmo.
 * Inclui as exceções de aprovação de campanhas por cliente confiável (W-CAMP).
 */
import * as React from 'react'
import { Link } from 'react-router-dom'
import { useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Landmark, Plus, Save, ShieldCheck, Trash2 } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { Separator } from '@/components/ui/separator'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { EmptyState, ErrorState, PageSkeleton } from '@/components/patterns/states'
import { useCan } from '@/hooks/usePermissions'
import { useInstanceApprovalMode } from '@/components/finance/approval-utils'
import { ApprovalErrorAlert } from '@/components/finance/approval-errors'
import { RequestFactorsForm } from '@/components/finance/request-factors-form'
import { useFinanceReady } from '@/components/finance/security-status'
import type { ApprovalRequest } from '@/pages/finance/api'
import { http, newRequestId } from '@/lib/http'
import { isApiError } from '@/lib/errors'
import { formatDateTime } from '@/lib/format'
import { SETTING_CATEGORY_LABELS, labelOf } from '@/components/admin/labels'
import { ReasonDialog } from '@/components/admin/ui'
import { OrgPicker, type PickedEntity } from '@/components/admin/pickers'
import { describeValue, parseSettingInput, settingInputValue, unitHint } from './settingValues'
import type { ApprovalPolicies, Organization, SettingItem } from '../types'

const CATEGORY_ORDER = ['campanhas', 'inventário', 'scheduler', 'players', 'proof-of-play', 'comercial', 'design', 'suporte', 'financeiro']

export default function SettingsPage() {
  const can = useCan()
  const single = useInstanceApprovalMode().isSingle
  const q = useQuery({ queryKey: ['settings', 'list'], queryFn: () => http.get<{ items: SettingItem[] }>('/settings').then((r) => r.items) })

  if (q.isLoading) return <PageSkeleton />
  if (q.error) return <ErrorState error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />
  const items = q.data ?? []
  const categories = Array.from(new Set(items.map((i) => i.category))).sort((a, b) => {
    const ia = CATEGORY_ORDER.indexOf(a)
    const ib = CATEGORY_ORDER.indexOf(b)
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib) || a.localeCompare(b)
  })

  return (
    <>
      <PageHeader title="Configurações do sistema" description="Parâmetros de negócio com histórico e auditoria. Valores padrão vêm do código." />
      <div className="flex flex-col gap-4 xl:gap-6">
        <Alert variant="info">
          <Landmark aria-hidden="true" />
          <div className="flex flex-col gap-1">
            <AlertTitle>{single ? 'Chaves financeiras exigem autorização' : 'Chaves financeiras exigem dupla aprovação'}</AlertTitle>
            <AlertDescription>
              {single
                ? 'Modelo e pool de comissão, margem mínima, cooldown de chave PIX, janela de reembolso e demais chaves marcadas como financeiras só mudam por pedido autorizado pelo Admin Master, com autenticador, senha financeira e código por e-mail.'
                : 'Modelo e pool de comissão, margem mínima, cooldown de chave PIX, janela de reembolso e demais chaves marcadas como financeiras só mudam por pedido aprovado pelos dois Admin Master.'}{' '}
              <Link to="/financeiro/aprovacoes" className="font-semibold text-link underline-offset-4 hover:underline">
                {single ? 'Ver aprovações' : 'Ver aprovações duplas'}
              </Link>
            </AlertDescription>
          </div>
        </Alert>
        {categories.map((cat) => (
          <Card key={cat}>
            <CardHeader>
              <CardTitle>{labelOf(SETTING_CATEGORY_LABELS, cat)}</CardTitle>
            </CardHeader>
            <div className="flex flex-col">
              {items
                .filter((i) => i.category === cat)
                .map((s, idx) => (
                  <React.Fragment key={s.key}>
                    {idx > 0 ? <Separator className="my-4" /> : null}
                    <SettingRow setting={s} canManage={can('settings.manage')} canDual={can('finance.approve')} />
                  </React.Fragment>
                ))}
            </div>
          </Card>
        ))}
        {can('campaigns.approve') ? <TrustedClientsCard /> : null}
      </div>
    </>
  )
}

function SettingRow({ setting, canManage, canDual }: { setting: SettingItem; canManage: boolean; canDual: boolean }) {
  const qc = useQueryClient()
  const [input, setInput] = React.useState(() => settingInputValue(setting.value))
  const [reason, setReason] = React.useState('')
  const [pending, setPending] = React.useState(false)
  // string = aviso/erro próprio desta tela; outro valor = erro da API (ex.: SECOND_MASTER_REQUIRED com atalho)
  const [error, setError] = React.useState<unknown>(null)
  const [dual, setDual] = React.useState(setting.financial)
  const [requested, setRequested] = React.useState<ApprovalRequest | null>(null)
  const single = useInstanceApprovalMode().isSingle
  const idemKey = React.useRef(newRequestId())

  React.useEffect(() => {
    setInput(settingInputValue(setting.value))
  }, [setting.value, setting.version])

  const parsed = parseSettingInput(setting, input)
  const changed = parsed.ok && JSON.stringify(parsed.value) !== JSON.stringify(setting.value)
  const reasonMin = dual ? 10 : 0
  const reasonOk = reason.trim().length >= reasonMin
  const fieldId = `setting-${setting.key.replace(/[^a-z0-9]/gi, '-')}`

  const save = async () => {
    if (!parsed.ok || !changed || !reasonOk) return
    setPending(true)
    setError(null)
    try {
      if (dual) {
        const r = await http.post<ApprovalRequest>(
          '/financial/approvals',
          { action_type: 'SETTING_CHANGE', payload: { key: setting.key, value: parsed.value, expected_version: setting.version }, reason: reason.trim() },
          { idempotencyKey: idemKey.current },
        )
        setRequested(r)
        idemKey.current = newRequestId()
        toast.success(r.approval_mode === 'SINGLE' ? 'Pedido criado. Autorize abaixo para aplicar.' : 'Pedido de dupla aprovação criado.')
      } else {
        await http.put(`/settings/${encodeURIComponent(setting.key)}`, { value: parsed.value, reason: reason.trim() || null, expected_version: setting.version })
        await qc.invalidateQueries({ queryKey: ['settings'] })
        setReason('')
        toast.success('Configuração salva.')
      }
    } catch (e) {
      if (isApiError(e) && e.code === 'SETTING_REQUIRES_DUAL_APPROVAL') {
        setDual(true)
        setError(
          single
            ? 'Esta configuração é financeira: a alteração precisa da sua autorização. Informe o motivo e solicite.'
            : 'Esta configuração é financeira: a alteração precisa de dupla aprovação dos Admin Master. Informe o motivo e solicite a aprovação.',
        )
      } else if (isApiError(e) && e.code === 'SETTING_VERSION_CONFLICT') {
        setError('A configuração foi alterada por outra pessoa. Os valores foram recarregados; revise e tente novamente.')
        void qc.invalidateQueries({ queryKey: ['settings'] })
      } else setError(e)
    } finally {
      setPending(false)
    }
  }

  return (
    <section aria-labelledby={`${fieldId}-title`} className="grid grid-cols-1 gap-4 lg:grid-cols-12">
      <div className="flex flex-col gap-1 lg:col-span-5">
        <h3 id={`${fieldId}-title`} className="text-sm font-semibold text-text">
          {setting.description}
        </h3>
        <p className="m-0 flex flex-wrap items-center gap-2 text-xs text-text-muted">
          <code>{setting.key}</code>
          {setting.financial || dual ? (
            <Badge tone="warning">
              <ShieldCheck aria-hidden="true" />
              {single ? 'Financeira · aprovação única' : 'Financeira · dupla aprovação'}
            </Badge>
          ) : null}
        </p>
        <p className="m-0 text-xs text-text-muted">
          Atual: <strong className="text-text">{describeValue(setting.key, setting.value)}</strong> · Padrão: {describeValue(setting.key, setting.default)}
          {setting.version ? ` · versão ${setting.version}, alterada em ${formatDateTime(setting.updated_at)}` : ' · nunca alterada'}
        </p>
      </div>
      <div className="flex flex-col gap-3 lg:col-span-7">
        {canManage ? (
          <form
            noValidate
            className="flex flex-col gap-3"
            onSubmit={(e) => {
              e.preventDefault()
              void save()
            }}
          >
            <SettingControl setting={setting} input={input} setInput={setInput} error={parsed.ok ? null : parsed.error} />
            {changed ? (
              <Field required={dual} invalid={!reasonOk && reason !== ''}>
                <FieldLabel>{dual ? 'Motivo do pedido (obrigatório)' : 'Motivo (opcional, vai para a auditoria)'}</FieldLabel>
                <Input value={reason} onChange={(e) => setReason(e.target.value)} maxLength={1000} />
                {dual ? (
                  <FieldDescription>
                    {single ? 'Mínimo de 10 caracteres. Fica registrado na auditoria.' : 'Mínimo de 10 caracteres. O outro Admin Master revisa o valor e o motivo.'}
                  </FieldDescription>
                ) : null}
              </Field>
            ) : null}
            {typeof error === 'string' && error ? (
              <Alert variant={dual ? 'warning' : 'danger'}>
                <AlertDescription className="text-text">{error}</AlertDescription>
              </Alert>
            ) : error ? (
              <ApprovalErrorAlert error={error} />
            ) : null}
            {requested && requested.approval_mode === 'SINGLE' && requested.status === 'PENDING' ? (
              <SingleAuthorizeInline
                request={requested}
                onDone={async () => {
                  setRequested(null)
                  setReason('')
                  await qc.invalidateQueries({ queryKey: ['settings'] })
                }}
              />
            ) : requested ? (
              <Alert variant="success">
                <div className="flex min-w-0 flex-1 flex-col gap-1">
                  <AlertTitle>Pedido de dupla aprovação criado</AlertTitle>
                  <AlertDescription>
                    Situação: {requested.status === 'PENDING' ? 'aguardando o outro Admin Master' : requested.status}. O valor só muda depois da aprovação.{' '}
                    <Link to="/financeiro/aprovacoes" className="font-semibold text-link underline-offset-4 hover:underline">
                      Acompanhar em Aprovações duplas
                    </Link>
                  </AlertDescription>
                </div>
              </Alert>
            ) : null}
            {dual && !canDual ? (
              <p className="m-0 text-xs text-text-muted">
                {single ? 'Somente os Administradores Master solicitam e autorizam' : 'Somente os dois Admin Master solicitam e aprovam'} alterações financeiras (permissão <code>finance.approve</code>).
              </p>
            ) : null}
            <div className="flex justify-end">
              <Button type="submit" variant={dual ? 'secondary' : 'primary'} loading={pending} disabled={!changed || !parsed.ok || !reasonOk || (dual && !canDual)}>
                {dual ? <ShieldCheck aria-hidden="true" /> : <Save aria-hidden="true" />}
                {dual ? (single ? 'Solicitar alteração (aprovação única)' : 'Solicitar alteração (dupla aprovação)') : 'Salvar'}
              </Button>
            </div>
          </form>
        ) : (
          <p className="text-sm text-text-muted">Somente leitura (permissão settings.manage necessária para alterar).</p>
        )}
      </div>
    </section>
  )
}

/** Aprovação ÚNICA: o pedido foi criado e o próprio Master o autoriza aqui mesmo (TOTP + senha financeira + código por e-mail). */
function SingleAuthorizeInline({ request, onDone }: { request: ApprovalRequest; onDone: () => void | Promise<void> }) {
  const { ready } = useFinanceReady(true)
  return (
    <div className="flex flex-col gap-3 rounded-lg border border-border p-4">
      <p className="m-0 text-sm font-semibold text-text">Pedido criado — falta a sua autorização</p>
      <p className="m-0 text-xs text-text-muted">
        O valor só muda depois que você autorizar. Se não autorizar agora, o pedido continua pendente em{' '}
        <Link to="/financeiro/aprovacoes" className="font-semibold text-link underline-offset-4 hover:underline">
          Financeiro → Aprovações
        </Link>
        .
      </p>
      <RequestFactorsForm request={request} kind="authorize" ready={ready} showTitle={false} onDone={() => void onDone()} />
    </div>
  )
}

function SettingControl({ setting, input, setInput, error }: { setting: SettingItem; input: string; setInput: (v: string) => void; error: string | null }) {
  const hint = unitHint(setting)
  if (setting.type === 'bool') {
    return (
      <Field orientation="horizontal" className="min-h-11">
        <Switch checked={input === 'true'} onCheckedChange={(c) => setInput(c ? 'true' : 'false')} />
        <FieldLabel className="font-normal">{input === 'true' ? 'Ativado' : 'Desativado'}</FieldLabel>
      </Field>
    )
  }
  if (setting.type === 'enum' && setting.choices) {
    const labelId = `${setting.key}-label`
    return (
      <Field>
        <FieldLabel id={labelId}>Novo valor</FieldLabel>
        {setting.choices.length <= 5 ? (
          <ToggleGroup type="single" aria-labelledby={labelId} value={input} onValueChange={(v) => v && setInput(v)} className="w-fit">
            {setting.choices.map((c) => (
              <ToggleGroupItem key={c} value={c}>
                {describeValue(setting.key, c)}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        ) : (
          <Select value={input} onValueChange={setInput}>
            <SelectTrigger aria-labelledby={labelId}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                {setting.choices.map((c) => (
                  <SelectItem key={c} value={c}>
                    {describeValue(setting.key, c)}
                  </SelectItem>
                ))}
              </SelectGroup>
            </SelectContent>
          </Select>
        )}
      </Field>
    )
  }
  return (
    <Field invalid={!!error}>
      <FieldLabel>Novo valor</FieldLabel>
      <Input
        value={input}
        onChange={(e) => setInput(e.target.value)}
        inputMode={setting.type === 'int' ? 'numeric' : undefined}
        className="max-w-xs"
      />
      {hint ? <FieldDescription>{hint}</FieldDescription> : null}
      <FieldError>{error}</FieldError>
    </Field>
  )
}

function TrustedClientsCard() {
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['campaigns', 'approval-policies'], queryFn: () => http.get<ApprovalPolicies>('/campaigns/approval-policies') })
  const orgs = useQueries({
    queries: (q.data?.items ?? []).map((i) => ({
      queryKey: ['organizations', 'detail', i.org_id],
      queryFn: () => http.get<Organization>(`/organizations/${i.org_id}`),
      staleTime: 5 * 60_000,
    })),
  })
  const nameOf = (id: string) => {
    const o = orgs.find((x) => x.data?.id === id)?.data
    return o ? o.trade_name || o.name : null
  }
  const [adding, setAdding] = React.useState(false)
  const [org, setOrg] = React.useState<PickedEntity | null>(null)
  const [mode, setMode] = React.useState<'AUTO' | 'MANUAL'>('AUTO')
  const [removing, setRemoving] = React.useState<string | null>(null)
  const invalidate = () => void qc.invalidateQueries({ queryKey: ['campaigns', 'approval-policies'] })

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Aprovação de campanhas — exceções por cliente confiável</CardTitle>
          <CardDescription>
            Modo global atual:{' '}
            <strong className="text-text">{q.data ? (q.data.global_mode === 'AUTO' ? 'automática' : 'manual (RD revisa)') : '—'}</strong> (chave{' '}
            <code>campaign.approval_mode</code> acima). As exceções valem por empresa anunciante.
          </CardDescription>
        </div>
        <Button
          onClick={() => {
            setOrg(null)
            setMode('AUTO')
            setAdding(true)
          }}
        >
          <Plus aria-hidden="true" />
          Nova exceção
        </Button>
      </CardHeader>
      {q.isLoading ? (
        <PageSkeleton label="Carregando exceções…" />
      ) : q.error ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} size="compact" />
      ) : !q.data?.items.length ? (
        <EmptyState size="compact" headingLevel="h3" title="Nenhuma exceção cadastrada." description="Todas as empresas seguem o modo global." />
      ) : (
        <div className="overflow-x-auto">
          <Table>
            <caption className="sr-only">Exceções de aprovação por cliente</caption>
            <TableHeader>
              <TableRow>
                <TableHead scope="col">Empresa</TableHead>
                <TableHead scope="col">Modo</TableHead>
                <TableHead scope="col">Motivo</TableHead>
                <TableHead scope="col">Atualizada em</TableHead>
                <TableHead scope="col" className="text-right">
                  Ações
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {q.data.items.map((i) => (
                <TableRow key={i.org_id}>
                  <TableCell>
                    <Link to={`/empresas/${i.org_id}`} className="font-semibold text-link underline-offset-4 hover:underline">
                      {nameOf(i.org_id) ?? `${i.org_id.slice(0, 8)}…`}
                    </Link>
                  </TableCell>
                  <TableCell>{i.mode === 'AUTO' ? <Badge tone="success">Automática (confiável)</Badge> : <Badge tone="neutral">Sempre manual</Badge>}</TableCell>
                  <TableCell className="max-w-[320px]">{i.reason ?? '—'}</TableCell>
                  <TableCell className="tabular-nums">{formatDateTime(i.updated_at)}</TableCell>
                  <TableCell className="text-right">
                    <Button variant="ghost" size="icon" aria-label={`Remover exceção de ${nameOf(i.org_id) ?? 'empresa'}`} onClick={() => setRemoving(i.org_id)}>
                      <Trash2 aria-hidden="true" />
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      <ReasonDialog
        open={adding}
        onOpenChange={setAdding}
        title="Nova exceção de aprovação"
        description="Defina como as campanhas pagas desta empresa são aprovadas."
        confirmLabel="Salvar exceção"
        onConfirm={async (reason) => {
          if (!org) throw new Error('Escolha a empresa anunciante.')
          await http.put(`/campaigns/approval-policies/${org.id}`, { mode, reason })
          invalidate()
          toast.success('Exceção de aprovação salva.')
        }}
      >
        <OrgPicker label="Empresa anunciante" role="ADVERTISER" value={org} onChange={setOrg} required />
        <Field>
          <FieldLabel id="exc-mode">Modo</FieldLabel>
          <ToggleGroup type="single" aria-labelledby="exc-mode" value={mode} onValueChange={(v) => v && setMode(v as 'AUTO' | 'MANUAL')} className="w-fit">
            <ToggleGroupItem value="AUTO">Automática (confiável)</ToggleGroupItem>
            <ToggleGroupItem value="MANUAL">Sempre manual</ToggleGroupItem>
          </ToggleGroup>
        </Field>
      </ReasonDialog>
      <ReasonDialog
        open={!!removing}
        onOpenChange={(o) => !o && setRemoving(null)}
        title={`Remover a exceção de ${removing ? nameOf(removing) ?? 'empresa' : ''}?`}
        consequences={['As próximas campanhas desta empresa seguem o modo global.']}
        minLength={0}
        reasonLabel="Observação"
        confirmLabel="Remover exceção"
        destructive
        onConfirm={async () => {
          if (!removing) return
          await http.delete(`/campaigns/approval-policies/${removing}`)
          invalidate()
          toast.success('Exceção removida.')
        }}
      />
    </Card>
  )
}

