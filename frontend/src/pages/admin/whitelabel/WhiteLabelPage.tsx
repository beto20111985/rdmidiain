/**
 * White-label / instância (`whitelabel.manage`) — GET|PATCH /instance/profile, PUT
 * /instance/logo, GET /instance/price-floors e PUT /instance/price-floors/{key}.
 * - Cores limitadas aos tokens `action` e `brand-blue` do DS, com contraste checado aqui e
 *   revalidado pelo servidor (BRAND_COLOR_CONTRAST).
 * - Subdomínio é informativo (roteamento no proxy de borda é fase futura).
 * - CloudPayPag: só a REFERÊNCIA ao segredo (env:/file:) — o valor nunca é exibido.
 * - Piso de preço RD: só Admin Master altera; histórico (rota de histórico ou auditoria).
 */
import * as React from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { History, ImageUp, KeyRound, Pencil, Plus, ShieldCheck } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { EmptyState, ErrorState, PageSkeleton } from '@/components/patterns/states'
import { useAuthStore } from '@/stores/authStore'
import { useCan } from '@/hooks/usePermissions'
import { http, type Paginated } from '@/lib/http'
import { errorMessage, isApiError } from '@/lib/errors'
import { formatCents, formatDateTime } from '@/lib/format'
import { validateFile } from '@/components/patterns/file-validation'
import { centsToInput, parseBrlToCents } from '@/components/admin/money'
import { useUserDirectory } from '@/components/admin/directory'
import { BRANDING_QUERY_KEY } from '@/components/admin/branding/query'
import { CANVAS_DARK, DEFAULT_ACCENT, DEFAULT_PRIMARY, HEX_RE, brandColorProblems, contrastRatio } from './contrast'
import type { AuditRow } from '../types'

interface InstanceProfile {
  instance_slug: string
  display_name: string
  is_white_label: boolean
  primary_color: string
  accent_color: string
  has_logo: boolean
  support_email: string | null
  support_phone: string | null
  legal_name: string | null
  document: string | null
  subdomain: string | null
  subdomain_note: string
  payment_provider: string
  payment_credentials_ref: string | null
  payment_account_label: string | null
  payment_credentials: { configured: boolean; ref_kind: string | null; detail: string | null }
  version: number
  isolation: { model: string; detail: string }
}

interface PriceFloor {
  key: string
  label: string
  unit: string
  min_price_cents: number
  note: string | null
  version: number
  updated_at: string | null
}

const SECRET_REF_RE = /^(env:[A-Z_][A-Z0-9_]{1,80}|file:\/run\/secrets\/[A-Za-z0-9_.-]{1,100})$/
const SUBDOMAIN_RE = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/

export default function WhiteLabelPage() {
  const q = useQuery({ queryKey: ['settings', 'instance-profile'], queryFn: () => http.get<InstanceProfile>('/instance/profile') })
  if (q.isLoading) return <PageSkeleton />
  if (q.error || !q.data) return <ErrorState error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />
  const p = q.data
  return (
    <>
      <PageHeader title="White-label" description={`Instância ${p.instance_slug} · versão de configuração ${p.version}`} />
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-12 xl:gap-6">
        <div className="flex flex-col gap-4 xl:col-span-8">
          <ProfileForm profile={p} />
          <PriceFloorsCard />
        </div>
        <div className="flex flex-col gap-4 xl:col-span-4">
          <LogoCard profile={p} />
          <Card>
            <CardHeader>
              <div className="flex flex-col gap-1">
                <CardTitle>Isolamento da instância</CardTitle>
                <CardDescription>{p.isolation.model === 'STACK_POR_INSTANCIA' ? 'Stack própria por instância' : p.isolation.model}</CardDescription>
              </div>
            </CardHeader>
            <p className="text-sm text-text-muted">{p.isolation.detail}</p>
          </Card>
        </div>
      </div>
    </>
  )
}

function ProfileForm({ profile }: { profile: InstanceProfile }) {
  const qc = useQueryClient()
  const init = React.useCallback(
    () => ({
      display_name: profile.display_name ?? '',
      legal_name: profile.legal_name ?? '',
      document: profile.document ?? '',
      is_white_label: profile.is_white_label,
      support_email: profile.support_email ?? '',
      support_phone: profile.support_phone ?? '',
      primary_color: profile.primary_color ?? DEFAULT_PRIMARY,
      accent_color: profile.accent_color ?? DEFAULT_ACCENT,
      subdomain: profile.subdomain ?? '',
      payment_credentials_ref: profile.payment_credentials_ref ?? '',
      payment_account_label: profile.payment_account_label ?? '',
    }),
    [profile],
  )
  const [v, setV] = React.useState(init)
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<{ message: string; field?: string } | null>(null)
  React.useEffect(() => setV(init()), [init])
  const set = <K extends keyof typeof v>(k: K, val: (typeof v)[K]) => setV((prev) => ({ ...prev, [k]: val }))
  const colors = brandColorProblems(v.primary_color, v.accent_color)
  const refProblem = v.payment_credentials_ref && !SECRET_REF_RE.test(v.payment_credentials_ref) ? 'Use env:NOME_DA_VARIAVEL ou file:/run/secrets/arquivo — nunca o valor do segredo.' : null
  const subProblem = v.subdomain && !SUBDOMAIN_RE.test(v.subdomain) ? 'Somente letras minúsculas, números e hífen (ex.: rede-sul).' : null
  const nameProblem = v.display_name.trim().length < 2 ? 'Informe o nome da instância.' : null
  const initial = init()
  const changes: Record<string, unknown> = {}
  for (const k of Object.keys(v) as Array<keyof typeof v>) {
    if (v[k] !== initial[k]) changes[k] = typeof v[k] === 'string' ? (v[k] as string).trim() || null : v[k]
  }
  const dirty = Object.keys(changes).length > 0
  const invalid = !!(colors.primary || colors.accent || refProblem || subProblem || nameProblem)

  const save = async () => {
    if (!dirty || invalid) return
    setPending(true)
    setError(null)
    try {
      await http.patch('/instance/profile', { ...changes, expected_version: profile.version })
      await qc.invalidateQueries({ queryKey: ['settings', 'instance-profile'] })
      // Marca pública (logo/nome/cores do painel) atualiza sem recarregar a página.
      await qc.invalidateQueries({ queryKey: BRANDING_QUERY_KEY })
      toast.success('Configuração da instância salva.')
    } catch (e) {
      if (isApiError(e) && e.code === 'INSTANCE_VERSION_CONFLICT') {
        setError({ message: 'A configuração foi alterada por outra pessoa. Recarregamos os dados; revise e salve de novo.' })
        void qc.invalidateQueries({ queryKey: ['settings', 'instance-profile'] })
      } else if (isApiError(e)) setError({ message: e.message, field: typeof e.details.field === 'string' ? e.details.field : undefined })
      else setError({ message: errorMessage(e) })
    } finally {
      setPending(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Marca e dados da instância</CardTitle>
          <CardDescription>Nome, cores (dentro dos tokens do design system), contato e referências desta instância.</CardDescription>
        </div>
      </CardHeader>
      <form
        noValidate
        className="flex flex-col gap-6"
        onSubmit={(e) => {
          e.preventDefault()
          void save()
        }}
      >
        <FieldGroup className="grid grid-cols-1 gap-5 md:grid-cols-2">
          <Field required invalid={!!nameProblem}>
            <FieldLabel>Nome exibido</FieldLabel>
            <Input value={v.display_name} onChange={(e) => set('display_name', e.target.value)} />
            <FieldError>{nameProblem}</FieldError>
          </Field>
          <Field>
            <FieldLabel>Razão social</FieldLabel>
            <Input value={v.legal_name} onChange={(e) => set('legal_name', e.target.value)} />
          </Field>
          <Field>
            <FieldLabel>CNPJ</FieldLabel>
            <Input value={v.document} onChange={(e) => set('document', e.target.value)} />
          </Field>
          <Field orientation="horizontal" className="min-h-11 self-end">
            <Switch checked={v.is_white_label} onCheckedChange={(c) => set('is_white_label', c)} />
            <FieldLabel className="font-normal">Instância white-label</FieldLabel>
          </Field>
          <Field>
            <FieldLabel>E-mail de suporte</FieldLabel>
            <Input type="email" value={v.support_email} onChange={(e) => set('support_email', e.target.value)} />
          </Field>
          <Field>
            <FieldLabel>Telefone de suporte</FieldLabel>
            <Input type="tel" value={v.support_phone} onChange={(e) => set('support_phone', e.target.value)} />
          </Field>
        </FieldGroup>

        <section aria-labelledby="wl-colors" className="flex flex-col gap-4">
          <h3 id="wl-colors" className="text-title-card text-text">
            Cores da marca
          </h3>
          <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
            <ColorField
              label="Cor principal (token action)"
              description="Botões preenchidos com texto branco. Contraste mínimo 4,5:1 com branco."
              value={v.primary_color}
              onChange={(c) => set('primary_color', c)}
              error={colors.primary ?? (error?.field === 'primary_color' ? error.message : null)}
              sample={
                <span className="inline-flex min-h-11 items-center rounded-md px-4 text-sm font-semibold" style={{ background: HEX_RE.test(v.primary_color) ? v.primary_color : undefined, color: '#FFFFFF' }}>
                  Botão principal
                </span>
              }
              ratio={HEX_RE.test(v.primary_color) ? contrastRatio(v.primary_color, '#FFFFFF') : null}
              onReset={() => set('primary_color', DEFAULT_PRIMARY)}
            />
            <ColorField
              label="Cor de destaque (token brand-blue)"
              description="Destaques e séries sobre o fundo escuro. Contraste mínimo 3:1 com o fundo."
              value={v.accent_color}
              onChange={(c) => set('accent_color', c)}
              error={colors.accent ?? (error?.field === 'accent_color' ? error.message : null)}
              sample={
                <span className="inline-flex min-h-11 items-center rounded-md px-4 text-sm font-semibold" style={{ background: CANVAS_DARK, color: HEX_RE.test(v.accent_color) ? v.accent_color : undefined }}>
                  Destaque sobre o fundo
                </span>
              }
              ratio={HEX_RE.test(v.accent_color) ? contrastRatio(v.accent_color, CANVAS_DARK) : null}
              onReset={() => set('accent_color', DEFAULT_ACCENT)}
            />
          </div>
        </section>

        <section aria-labelledby="wl-domain" className="flex flex-col gap-4">
          <h3 id="wl-domain" className="text-title-card text-text">
            Subdomínio (informativo)
          </h3>
          <Field invalid={!!subProblem}>
            <FieldLabel>Subdomínio</FieldLabel>
            <Input value={v.subdomain} onChange={(e) => set('subdomain', e.target.value.toLowerCase())} className="max-w-sm" spellCheck={false} />
            <FieldDescription>{profile.subdomain_note}</FieldDescription>
            <FieldError>{subProblem}</FieldError>
          </Field>
        </section>

        <section aria-labelledby="wl-pay" className="flex flex-col gap-4">
          <h3 id="wl-pay" className="flex flex-wrap items-center gap-2 text-title-card text-text">
            <KeyRound aria-hidden="true" className="size-4" />
            Pagamentos ({profile.payment_provider})
          </h3>
          <Alert variant={profile.payment_credentials.configured ? 'success' : 'warning'}>
            <div className="flex min-w-0 flex-1 flex-col gap-1">
              <AlertTitle>{profile.payment_credentials.configured ? 'Segredo da CloudPayPag encontrado neste ambiente' : 'Segredo da CloudPayPag não disponível'}</AlertTitle>
              <AlertDescription>
                {profile.payment_credentials.detail ?? `Referência do tipo ${profile.payment_credentials.ref_kind ?? '—'}. O valor do segredo nunca é exibido nem enviado ao navegador.`}
              </AlertDescription>
            </div>
          </Alert>
          <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
            <Field invalid={!!refProblem}>
              <FieldLabel>Referência ao segredo</FieldLabel>
              <Input value={v.payment_credentials_ref} onChange={(e) => set('payment_credentials_ref', e.target.value)} spellCheck={false} autoComplete="off" />
              <FieldDescription>Somente a referência: env:NOME ou file:/run/secrets/arquivo.</FieldDescription>
              <FieldError>{refProblem}</FieldError>
            </Field>
            <Field>
              <FieldLabel>Identificação da conta</FieldLabel>
              <Input value={v.payment_account_label} onChange={(e) => set('payment_account_label', e.target.value)} />
            </Field>
          </div>
        </section>

        {error && !error.field ? (
          <Alert variant="danger">
            <AlertDescription className="text-text">{error.message}</AlertDescription>
          </Alert>
        ) : null}
        <div className="flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
          <Button variant="secondary" onClick={() => setV(init())} disabled={!dirty || pending}>
            Descartar alterações
          </Button>
          <Button type="submit" loading={pending} disabled={!dirty || invalid}>
            Salvar
          </Button>
        </div>
      </form>
    </Card>
  )
}

function ColorField({
  label,
  description,
  value,
  onChange,
  error,
  sample,
  ratio,
  onReset,
}: {
  label: string
  description: string
  value: string
  onChange: (v: string) => void
  error: string | null
  sample: React.ReactNode
  ratio: number | null
  onReset: () => void
}) {
  const id = React.useId()
  return (
    <Field invalid={!!error}>
      <FieldLabel>{label}</FieldLabel>
      <div className="flex flex-wrap items-center gap-3">
        <input
          type="color"
          aria-label={`${label} — seletor`}
          value={HEX_RE.test(value) ? value : '#000000'}
          onChange={(e) => onChange(e.target.value.toUpperCase())}
          className="h-11 w-14 cursor-pointer rounded-md border border-control-border bg-field p-1"
        />
        <Input value={value} onChange={(e) => onChange(e.target.value.toUpperCase())} className="w-32 font-mono" spellCheck={false} aria-describedby={id} />
        <Button variant="tertiary" onClick={onReset}>
          Padrão do DS
        </Button>
      </div>
      <div className="flex flex-wrap items-center gap-3">
        {sample}
        <span id={id} className={ratio !== null && !error ? 'text-xs text-success' : 'text-xs text-text-muted'}>
          {ratio !== null ? `Contraste ${ratio.toFixed(2)}:1` : 'Cor inválida'}
        </span>
      </div>
      <FieldDescription>{description}</FieldDescription>
      <FieldError>{error}</FieldError>
    </Field>
  )
}

function LogoCard({ profile }: { profile: InstanceProfile }) {
  const qc = useQueryClient()
  const inputRef = React.useRef<HTMLInputElement>(null)
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)
  const upload = async (file: File) => {
    const problem = validateFile(file, ['image/png', 'image/jpeg'], 1024 * 1024)
    if (problem) {
      setError(`${problem} Use PNG ou JPG de até 1 MB (SVG não é aceito).`)
      return
    }
    setPending(true)
    setError(null)
    try {
      const form = new FormData()
      form.append('file', file)
      await http.put('/instance/logo', form)
      await qc.invalidateQueries({ queryKey: ['settings', 'instance-profile'] })
      // Marca pública (logo/nome/cores do painel) atualiza sem recarregar a página.
      await qc.invalidateQueries({ queryKey: BRANDING_QUERY_KEY })
      toast.success('Logo atualizada.')
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setPending(false)
    }
  }
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Logo da instância</CardTitle>
          <CardDescription>Usada na tela de entrada, nos PDFs e nos e-mails. PNG ou JPG até 1 MB.</CardDescription>
        </div>
      </CardHeader>
      <div className="flex min-h-[120px] items-center justify-center rounded-lg border border-border bg-bg p-4">
        {profile.has_logo ? (
          <img src={`/api/v1/instance/logo?v=${profile.version}`} alt={`Logo de ${profile.display_name}`} className="max-h-24 max-w-full object-contain" />
        ) : (
          <p className="text-sm text-text-muted">Nenhuma logo enviada — usando a marca padrão.</p>
        )}
      </div>
      <input
        ref={inputRef}
        type="file"
        accept="image/png,image/jpeg"
        className="sr-only"
        tabIndex={-1}
        aria-label="Arquivo da logo"
        onChange={(e) => {
          const f = e.target.files?.[0]
          if (f) void upload(f)
          e.target.value = ''
        }}
      />
      {error ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">{error}</AlertDescription>
        </Alert>
      ) : null}
      <Button variant="secondary" onClick={() => inputRef.current?.click()} loading={pending}>
        <ImageUp aria-hidden="true" />
        {profile.has_logo ? 'Trocar logo' : 'Enviar logo'}
      </Button>
    </Card>
  )
}

function PriceFloorsCard() {
  const isMaster = useAuthStore((s) => !!s.me?.user.is_admin_master)
  const qc = useQueryClient()
  const q = useQuery({ queryKey: ['pricing', 'price-floors'], queryFn: () => http.get<{ items: PriceFloor[] }>('/instance/price-floors').then((r) => r.items) })
  const [editing, setEditing] = React.useState<PriceFloor | 'new' | null>(null)
  const [history, setHistory] = React.useState<PriceFloor | null>(null)
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Piso de preço RD</CardTitle>
          <CardDescription>Preço mínimo da RD por item: nenhuma tabela da instância pode ficar abaixo deste valor.</CardDescription>
        </div>
        {isMaster ? (
          <Button onClick={() => setEditing('new')}>
            <Plus aria-hidden="true" />
            Novo piso
          </Button>
        ) : null}
      </CardHeader>
      {!isMaster ? (
        <Alert variant="neutral">
          <ShieldCheck aria-hidden="true" />
          <AlertDescription>Somente os dois Admin Master da RD alteram o piso. Você pode consultar os valores e o histórico.</AlertDescription>
        </Alert>
      ) : null}
      {q.isLoading ? (
        <PageSkeleton label="Carregando pisos…" />
      ) : q.error ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} size="compact" />
      ) : !q.data?.length ? (
        <EmptyState size="compact" headingLevel="h3" title="Nenhum piso cadastrado." description={isMaster ? 'Cadastre o preço mínimo por item vendido.' : undefined} />
      ) : (
        <div className="overflow-x-auto">
          <Table>
            <caption className="sr-only">Pisos de preço</caption>
            <TableHeader>
              <TableRow>
                <TableHead scope="col">Item</TableHead>
                <TableHead scope="col" numeric>
                  Mínimo
                </TableHead>
                <TableHead scope="col">Versão</TableHead>
                <TableHead scope="col">Atualizado</TableHead>
                <TableHead scope="col" className="text-right">
                  Ações
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {q.data.map((f) => (
                <TableRow key={f.key}>
                  <TableCell>
                    <span className="font-semibold">{f.label}</span>
                    <span className="block text-xs text-text-muted">
                      <code>{f.key}</code> · por {f.unit}
                    </span>
                  </TableCell>
                  <TableCell numeric className="tabular-nums">
                    {formatCents(f.min_price_cents)}
                  </TableCell>
                  <TableCell className="tabular-nums">{f.version}</TableCell>
                  <TableCell className="tabular-nums">{formatDateTime(f.updated_at)}</TableCell>
                  <TableCell className="text-right">
                    <div className="flex justify-end gap-1">
                      <Button variant="ghost" onClick={() => setHistory(f)}>
                        <History aria-hidden="true" />
                        Histórico
                      </Button>
                      {isMaster ? (
                        <Button variant="ghost" size="icon" aria-label={`Alterar piso de ${f.label}`} onClick={() => setEditing(f)}>
                          <Pencil aria-hidden="true" />
                        </Button>
                      ) : null}
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      <FloorDialog floor={editing} onOpenChange={(o) => !o && setEditing(null)} onDone={() => void qc.invalidateQueries({ queryKey: ['pricing', 'price-floors'] })} />
      <FloorHistoryDialog floor={history} onOpenChange={(o) => !o && setHistory(null)} />
    </Card>
  )
}

function FloorDialog({ floor, onOpenChange, onDone }: { floor: PriceFloor | 'new' | null; onOpenChange: (o: boolean) => void; onDone: () => void }) {
  const existing = floor && floor !== 'new' ? floor : null
  const [key, setKey] = React.useState('')
  const [label, setLabel] = React.useState('')
  const [unit, setUnit] = React.useState('')
  const [price, setPrice] = React.useState('')
  const [note, setNote] = React.useState('')
  const [reason, setReason] = React.useState('')
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)
  React.useEffect(() => {
    setKey(existing?.key ?? '')
    setLabel(existing?.label ?? '')
    setUnit(existing?.unit ?? '')
    setPrice(existing ? centsToInput(existing.min_price_cents) : '')
    setNote(existing?.note ?? '')
    setReason('')
    setError(null)
  }, [floor, existing])
  const cents = parseBrlToCents(price)
  const keyOk = /^[a-z0-9][a-z0-9_.-]{1,79}$/.test(key)
  const ok = keyOk && label.trim().length >= 2 && unit.trim().length >= 1 && cents !== null && reason.trim().length >= 5
  return (
    <Dialog open={floor !== null} onOpenChange={(o) => (!pending ? onOpenChange(o) : undefined)}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>{existing ? `Alterar piso: ${existing.label}` : 'Novo piso de preço'}</DialogTitle>
          <DialogDescription>Alteração exclusiva dos Admin Master da RD, registrada no histórico e na auditoria.</DialogDescription>
        </DialogHeader>
        <form
          noValidate
          className="flex flex-col gap-5"
          onSubmit={async (e) => {
            e.preventDefault()
            if (!ok || cents === null) return
            setPending(true)
            setError(null)
            try {
              await http.put(`/instance/price-floors/${encodeURIComponent(key)}`, {
                label: label.trim(),
                unit: unit.trim(),
                min_price_cents: cents,
                note: note.trim() || null,
                reason: reason.trim(),
                expected_version: existing ? existing.version : undefined,
              })
              toast.success('Piso de preço salvo.')
              onDone()
              onOpenChange(false)
            } catch (err) {
              setError(errorMessage(err))
            } finally {
              setPending(false)
            }
          }}
        >
          <FieldGroup>
            <Field required invalid={key !== '' && !keyOk}>
              <FieldLabel>Chave</FieldLabel>
              <Input value={key} onChange={(e) => setKey(e.target.value.toLowerCase())} disabled={!!existing} spellCheck={false} />
              <FieldDescription>Ex.: insercao.30s.mensal — minúsculas, números, ponto, hífen e “_”.</FieldDescription>
            </Field>
            <Field required>
              <FieldLabel>Descrição do item</FieldLabel>
              <Input value={label} onChange={(e) => setLabel(e.target.value)} />
            </Field>
            <Field required>
              <FieldLabel>Unidade</FieldLabel>
              <Input value={unit} onChange={(e) => setUnit(e.target.value)} placeholder="ex.: tela/mês" />
            </Field>
            <Field required invalid={price !== '' && cents === null}>
              <FieldLabel>Preço mínimo (R$)</FieldLabel>
              <Input inputMode="decimal" value={price} onChange={(e) => setPrice(e.target.value)} placeholder="0,00" className="max-w-[200px]" />
              <FieldDescription>{cents !== null ? `= ${formatCents(cents)}` : 'Use vírgula para os centavos.'}</FieldDescription>
              <FieldError>{price !== '' && cents === null ? 'Valor inválido.' : undefined}</FieldError>
            </Field>
            <Field>
              <FieldLabel>Observação</FieldLabel>
              <Input value={note} onChange={(e) => setNote(e.target.value)} />
            </Field>
            <Field required invalid={reason !== '' && reason.trim().length < 5}>
              <FieldLabel>Motivo da alteração</FieldLabel>
              <Input value={reason} onChange={(e) => setReason(e.target.value)} />
              <FieldDescription>Mínimo de 5 caracteres.</FieldDescription>
            </Field>
          </FieldGroup>
          {error ? (
            <Alert variant="danger">
              <AlertDescription className="text-text">{error}</AlertDescription>
            </Alert>
          ) : null}
          <DialogFooter>
            <Button variant="secondary" onClick={() => onOpenChange(false)} disabled={pending}>
              Cancelar
            </Button>
            <Button type="submit" loading={pending} disabled={!ok}>
              Salvar piso
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

interface FloorHistoryItem {
  at: string | null
  by: string | null
  old: number | null
  value: number | null
  reason: string | null
  version: number | null
}

function FloorHistoryDialog({ floor, onOpenChange }: { floor: PriceFloor | null; onOpenChange: (o: boolean) => void }) {
  const can = useCan()
  const directory = useUserDirectory()
  const q = useQuery({
    queryKey: ['pricing', 'price-floors', floor?.key, 'history'],
    enabled: !!floor,
    retry: false,
    queryFn: async (): Promise<{ items: FloorHistoryItem[]; source: 'history' | 'audit' }> => {
      const key = floor!.key
      try {
        const r = await http.get<{ items: Array<Record<string, unknown>> }>(`/instance/price-floors/${encodeURIComponent(key)}/history`)
        const num = (x: unknown) => (typeof x === 'number' ? x : null)
        const str = (x: unknown) => (typeof x === 'string' ? x : null)
        return {
          source: 'history',
          items: (r.items ?? []).map((h) => ({
            at: str(h.changed_at) ?? str(h.created_at),
            by: str(h.changed_by_name) ?? str(h.changed_by),
            old: num(h.old_min_price_cents),
            value: num(h.new_min_price_cents) ?? num(h.min_price_cents),
            reason: str(h.reason),
            version: num(h.version),
          })),
        }
      } catch (e) {
        if (!(isApiError(e) && (e.status === 404 || e.status === 405))) throw e
      }
      if (!can('audit.view')) throw new Error('Histórico indisponível: a rota de histórico ainda não existe e você não tem acesso à auditoria.')
      const a = await http.get<Paginated<AuditRow>>('/audit', { query: { entity_type: 'rd_price_floor', entity_id: key, page_size: 100 } })
      return {
        source: 'audit',
        items: a.items.map((r) => {
          const b = (r.before ?? {}) as Record<string, unknown>
          const af = (r.after ?? {}) as Record<string, unknown>
          return {
            at: r.created_at,
            by: r.actor_user_id,
            old: typeof b.min_price_cents === 'number' ? b.min_price_cents : null,
            value: typeof af.min_price_cents === 'number' ? af.min_price_cents : null,
            reason: r.reason,
            version: typeof af.version === 'number' ? af.version : null,
          }
        }),
      }
    },
  })
  return (
    <Dialog open={!!floor} onOpenChange={onOpenChange}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>Histórico do piso: {floor?.label}</DialogTitle>
          <DialogDescription>{q.data?.source === 'audit' ? 'Fonte: trilha de auditoria.' : 'Alterações registradas (somente inclusão).'}</DialogDescription>
        </DialogHeader>
        {q.isLoading ? (
          <PageSkeleton label="Carregando histórico…" />
        ) : q.error ? (
          <ErrorState error={q.error} onRetry={() => void q.refetch()} size="compact" />
        ) : !q.data?.items.length ? (
          <EmptyState size="compact" headingLevel="h3" title="Sem alterações registradas." />
        ) : (
          <ol className="m-0 flex list-none flex-col gap-3 p-0">
            {q.data.items.map((h, i) => (
              <li key={i} className="flex flex-col gap-1 rounded-lg border border-border p-3 text-sm">
                <span className="flex flex-wrap items-center gap-2 font-semibold">
                  {h.old !== null ? `${formatCents(h.old)} → ` : ''}
                  {formatCents(h.value)}
                  {h.version !== null ? <Badge tone="neutral">versão {h.version}</Badge> : null}
                </span>
                <span className="text-xs text-text-muted">
                  {formatDateTime(h.at)} · {(h.by && directory.nameOf(h.by)) ?? h.by ?? '—'}
                </span>
                {h.reason ? <span className="text-text">{h.reason}</span> : null}
              </li>
            ))}
          </ol>
        )}
        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Fechar
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

