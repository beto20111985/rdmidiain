/**
 * Formulários das etapas do onboarding (compartilhados entre o wizard da equipe RD e o
 * auto-cadastro público). Cada etapa valida no cliente e salva via `onSave` (PUT .../steps/{step});
 * erros 422 do servidor voltam por campo.
 */
import * as React from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Switch } from '@/components/ui/switch'
import { Checkbox } from '@/components/ui/checkbox'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { OptionSelect } from '@/components/ops/filter-select'
import { OrganizationPicker, TechnicianSelect } from '@/components/ops/pickers'
import { centsToInput, parseBrlToCents } from '@/components/ops/utils'
import { isApiError, errorMessage } from '@/lib/errors'
import { useQuery } from '@tanstack/react-query'
import { qk } from '@/lib/queryKeys'
import { establishmentsApi } from '@/pages/ops/api/client'
import { usePartnerPlans, useSegments, useTaxonomyNodes } from '@/pages/ops/api/hooks'
import { TV_PROVISION_LABEL, UFS } from '@/pages/ops/api/labels'
import { docError } from './stepRules'

type Data = Record<string, unknown>
export interface StepFormProps {
  initial: Data | undefined
  staff: boolean
  disabled?: boolean
  onSave: (data: Data) => Promise<unknown>
  saveLabel?: string
}

const str = (v: unknown) => (typeof v === 'string' ? v : v === null || v === undefined ? '' : String(v))
const nul = (v: string) => (v.trim() ? v.trim() : null)

/** Estado de envio + erros do servidor por campo, comum a todas as etapas. */
function useStepSubmit(onSave: StepFormProps['onSave']) {
  const [pending, setPending] = React.useState(false)
  const [serverErrors, setServerErrors] = React.useState<Record<string, string>>({})
  const [general, setGeneral] = React.useState<string | null>(null)
  const submit = async (data: Data) => {
    setPending(true)
    setServerErrors({})
    setGeneral(null)
    try {
      await onSave(data)
    } catch (e) {
      if (isApiError(e)) {
        const fe = e.fieldErrors
        const mapped: Record<string, string> = {}
        for (const [k, v] of Object.entries(fe)) mapped[k.split('.').pop() ?? k] = v
        setServerErrors(mapped)
        setGeneral(Object.keys(mapped).length ? 'Revise os campos destacados.' : e.message)
      } else setGeneral(errorMessage(e))
    } finally {
      setPending(false)
    }
  }
  return { pending, serverErrors, general, submit }
}

function Footer({ pending, general, label, disabled }: { pending: boolean; general: string | null; label?: string; disabled?: boolean }) {
  return (
    <div className="flex flex-col gap-3">
      {general ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">{general}</AlertDescription>
        </Alert>
      ) : null}
      {!disabled ? (
        <Button type="submit" className="w-fit" loading={pending} loadingText="Salvando…">
          {label ?? 'Salvar e continuar'}
        </Button>
      ) : null}
    </div>
  )
}

function DocFields({
  docType,
  setDocType,
  doc,
  setDoc,
  err,
  disabled,
}: {
  docType: string | null
  setDocType: (v: string | null) => void
  doc: string
  setDoc: (v: string) => void
  err?: string | null
  disabled?: boolean
}) {
  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-[140px_1fr]">
      <Field>
        <FieldLabel>Tipo</FieldLabel>
        <OptionSelect
          value={docType}
          onChange={setDocType}
          disabled={disabled}
          options={[
            { value: 'CNPJ', label: 'CNPJ' },
            { value: 'CPF', label: 'CPF' },
          ]}
        />
      </Field>
      <Field invalid={!!err}>
        <FieldLabel>CPF/CNPJ</FieldLabel>
        <Input value={doc} onChange={(e) => setDoc(e.target.value)} disabled={disabled} autoComplete="off" />
        <FieldError>{err}</FieldError>
      </Field>
    </div>
  )
}

// --------------------------------------------------------------------------- Empresa
export function CompanyStepForm({ initial, staff, disabled, onSave, saveLabel }: StepFormProps) {
  const [existing, setExisting] = React.useState<string | null>((initial?.existing_org_id as string) ?? null)
  const [name, setName] = React.useState(str(initial?.name))
  const [trade, setTrade] = React.useState(str(initial?.trade_name))
  const [docType, setDocType] = React.useState<string | null>((initial?.document_type as string) ?? 'CNPJ')
  const [doc, setDoc] = React.useState(str(initial?.document))
  const [resp, setResp] = React.useState(str(initial?.responsible_name))
  const [email, setEmail] = React.useState(str(initial?.email))
  const [phone, setPhone] = React.useState(str(initial?.phone))
  const [touched, setTouched] = React.useState(false)
  const s = useStepSubmit(onSave)
  const errs = {
    name: !existing && name.trim().length < 2 ? 'Informe a razão social ou o nome.' : s.serverErrors.name ?? null,
    document: !existing ? (docError(doc, docType, true) ?? s.serverErrors.document ?? null) : null,
    email: email.trim() && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email.trim()) ? 'E-mail inválido.' : s.serverErrors.email ?? null,
  }
  return (
    <form
      noValidate
      className="flex flex-col gap-5"
      onSubmit={(e) => {
        e.preventDefault()
        setTouched(true)
        if (Object.values(errs).some(Boolean)) return
        void s.submit(
          existing
            ? { existing_org_id: existing }
            : {
                name: name.trim(),
                trade_name: nul(trade),
                document_type: docType,
                document: doc.trim(),
                responsible_name: nul(resp),
                email: nul(email),
                phone: nul(phone),
              },
        )
      }}
    >
      {staff ? (
        <OrganizationPicker
          value={existing}
          onChange={setExisting}
          legend="Vincular a uma empresa já cadastrada (opcional)"
          description="Deixe em branco para cadastrar uma empresa nova com os dados abaixo."
        />
      ) : null}
      {!existing ? (
        <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
          <Field invalid={touched && !!errs.name} required>
            <FieldLabel>Razão social ou nome</FieldLabel>
            <Input value={name} onChange={(e) => setName(e.target.value)} disabled={disabled} autoComplete="organization" />
            <FieldError>{touched ? errs.name : null}</FieldError>
          </Field>
          <Field>
            <FieldLabel>Nome fantasia</FieldLabel>
            <Input value={trade} onChange={(e) => setTrade(e.target.value)} disabled={disabled} />
          </Field>
          <DocFields docType={docType} setDocType={setDocType} doc={doc} setDoc={setDoc} err={touched ? errs.document : null} disabled={disabled} />
          <Field>
            <FieldLabel>Responsável</FieldLabel>
            <Input value={resp} onChange={(e) => setResp(e.target.value)} disabled={disabled} autoComplete="name" />
          </Field>
          <Field invalid={touched && !!errs.email}>
            <FieldLabel>E-mail</FieldLabel>
            <Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} disabled={disabled} autoComplete="email" />
            <FieldError>{touched ? errs.email : null}</FieldError>
          </Field>
          <Field>
            <FieldLabel>Telefone</FieldLabel>
            <Input type="tel" value={phone} onChange={(e) => setPhone(e.target.value)} disabled={disabled} autoComplete="tel" />
          </Field>
        </div>
      ) : null}
      <Footer pending={s.pending} general={s.general} label={saveLabel} disabled={disabled} />
    </form>
  )
}

// --------------------------------------------------------------------------- Estabelecimento + categorias
export function EstablishmentStepForm({ initial, staff, disabled, onSave, saveLabel }: StepFormProps) {
  const [name, setName] = React.useState(str(initial?.name))
  const [trade, setTrade] = React.useState(str(initial?.trade_name))
  const [legal, setLegal] = React.useState(str(initial?.legal_name))
  const [docType, setDocType] = React.useState<string | null>((initial?.document_type as string) ?? 'CNPJ')
  const [doc, setDoc] = React.useState(str(initial?.document))
  const [resp, setResp] = React.useState(str(initial?.responsible_name))
  const [phone, setPhone] = React.useState(str(initial?.phone))
  const [email, setEmail] = React.useState(str(initial?.email))
  const [category, setCategory] = React.useState<string | null>((initial?.category_id as string) ?? null)
  const [segments, setSegments] = React.useState<string[]>(Array.isArray(initial?.segment_ids) ? (initial!.segment_ids as string[]) : [])
  const [touched, setTouched] = React.useState(false)
  const s = useStepSubmit(onSave)
  const errs = {
    name: name.trim().length < 2 ? 'Informe o nome do estabelecimento.' : s.serverErrors.name ?? null,
    responsible_name: resp.trim().length < 2 ? 'Informe o nome do responsável.' : s.serverErrors.responsible_name ?? null,
    phone: phone.trim().length < 8 ? 'Informe um telefone com DDD.' : s.serverErrors.phone ?? null,
    document: docError(doc, docType, false) ?? s.serverErrors.document ?? null,
    email: email.trim() && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email.trim()) ? 'E-mail inválido.' : s.serverErrors.email ?? null,
  }
  return (
    <form
      noValidate
      className="flex flex-col gap-5"
      onSubmit={(e) => {
        e.preventDefault()
        setTouched(true)
        if (Object.values(errs).some(Boolean)) return
        const body: Data = {
          name: name.trim(),
          trade_name: nul(trade),
          legal_name: nul(legal),
          document_type: doc.trim() ? docType : null,
          document: nul(doc),
          responsible_name: resp.trim(),
          phone: phone.trim(),
          email: nul(email),
        }
        if (staff) {
          body.category_id = category
          body.segment_ids = segments
        }
        void s.submit(body)
      }}
    >
      <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
        <Field invalid={touched && !!errs.name} required>
          <FieldLabel>Nome do estabelecimento</FieldLabel>
          <Input value={name} onChange={(e) => setName(e.target.value)} disabled={disabled} />
          <FieldError>{touched ? errs.name : null}</FieldError>
        </Field>
        <Field>
          <FieldLabel>Nome fantasia</FieldLabel>
          <Input value={trade} onChange={(e) => setTrade(e.target.value)} disabled={disabled} />
        </Field>
        <Field>
          <FieldLabel>Razão social</FieldLabel>
          <Input value={legal} onChange={(e) => setLegal(e.target.value)} disabled={disabled} />
        </Field>
        <DocFields docType={docType} setDocType={setDocType} doc={doc} setDoc={setDoc} err={touched ? errs.document : null} disabled={disabled} />
        <Field invalid={touched && !!errs.responsible_name} required>
          <FieldLabel>Responsável no local</FieldLabel>
          <Input value={resp} onChange={(e) => setResp(e.target.value)} disabled={disabled} autoComplete="name" />
          <FieldError>{touched ? errs.responsible_name : null}</FieldError>
        </Field>
        <Field invalid={touched && !!errs.phone} required>
          <FieldLabel>Telefone</FieldLabel>
          <Input type="tel" value={phone} onChange={(e) => setPhone(e.target.value)} disabled={disabled} autoComplete="tel" />
          <FieldError>{touched ? errs.phone : null}</FieldError>
        </Field>
        <Field invalid={touched && !!errs.email}>
          <FieldLabel>E-mail do local</FieldLabel>
          <Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} disabled={disabled} autoComplete="email" />
          <FieldError>{touched ? errs.email : null}</FieldError>
        </Field>
      </div>
      {staff ? (
        <ClassificationFields category={category} setCategory={setCategory} segments={segments} setSegments={setSegments} disabled={disabled} />
      ) : (
        <p className="m-0 text-sm text-text-muted">A categoria e os segmentos do seu negócio são definidos pela equipe RD na análise do cadastro.</p>
      )}
      <Footer pending={s.pending} general={s.general} label={saveLabel} disabled={disabled} />
    </form>
  )
}

/** Categoria e segmentos — só para a equipe RD (a taxonomia exige autenticação). */
function ClassificationFields({
  category,
  setCategory,
  segments,
  setSegments,
  disabled,
}: {
  category: string | null
  setCategory: (v: string | null) => void
  segments: string[]
  setSegments: React.Dispatch<React.SetStateAction<string[]>>
  disabled?: boolean
}) {
  const nodes = useTaxonomyNodes()
  const segs = useSegments()
  return (
    <div className="flex flex-col gap-4">
      <Field className="max-w-md">
        <FieldLabel>Categoria (ramo)</FieldLabel>
        <OptionSelect
          value={category}
          onChange={setCategory}
          includeNone
          noneLabel="Sem categoria"
          disabled={disabled}
          options={(nodes.data?.items ?? []).map((n) => ({ value: n.id, label: `${'— '.repeat(n.depth)}${n.name}` }))}
        />
      </Field>
      <fieldset className="m-0 flex max-h-56 flex-col overflow-y-auto border-0 p-0">
        <legend className="mb-1 text-sm font-semibold text-text">Segmentos</legend>
        {(segs.data?.items ?? []).map((sg) => (
          <Field key={sg.id} orientation="horizontal" className="min-h-11" style={{ paddingLeft: `${sg.depth * 16}px` }}>
            <Checkbox
              checked={segments.includes(sg.id)}
              disabled={disabled}
              onCheckedChange={(c) => setSegments((p) => (c ? [...p, sg.id] : p.filter((x) => x !== sg.id)))}
            />
            <FieldLabel className="font-normal">{sg.name}</FieldLabel>
          </Field>
        ))}
        {!segs.data?.items.length ? <p className="m-0 text-sm text-text-muted">Nenhum segmento cadastrado.</p> : null}
      </fieldset>
    </div>
  )
}

// --------------------------------------------------------------------------- Endereço
export function AddressStepForm({ initial, staff, disabled, onSave, saveLabel }: StepFormProps) {
  const [cep, setCep] = React.useState(str(initial?.postal_code))
  const [address, setAddress] = React.useState(str(initial?.address))
  const [number, setNumber] = React.useState(str(initial?.number))
  const [complement, setComplement] = React.useState(str(initial?.complement))
  const [nb, setNb] = React.useState(str(initial?.neighborhood))
  const [city, setCity] = React.useState(str(initial?.city))
  const [uf, setUf] = React.useState<string | null>((initial?.state as string) ?? null)
  const [tz, setTz] = React.useState(str(initial?.timezone))
  const [touched, setTouched] = React.useState(false)
  const s = useStepSubmit(onSave)
  const sugg = useQuery({
    queryKey: qk.sub('establishments', 'timezone-suggestion', uf ?? '', city.trim()),
    queryFn: () => establishmentsApi.timezoneSuggestion(uf!, city.trim() || undefined),
    enabled: staff && !!uf,
    staleTime: Infinity,
    retry: false,
  })
  const errs = {
    address: address.trim().length < 2 ? 'Informe o logradouro.' : s.serverErrors.address ?? null,
    city: city.trim().length < 2 ? 'Informe a cidade.' : s.serverErrors.city ?? null,
    state: !uf ? 'Selecione a UF.' : s.serverErrors.state ?? null,
    postal_code: cep.trim() && cep.replace(/\D/g, '').length !== 8 ? 'CEP com 8 dígitos.' : s.serverErrors.postal_code ?? null,
  }
  return (
    <form
      noValidate
      className="flex flex-col gap-5"
      onSubmit={(e) => {
        e.preventDefault()
        setTouched(true)
        if (Object.values(errs).some(Boolean)) return
        const body: Data = {
          address: address.trim(),
          number: nul(number),
          complement: nul(complement),
          neighborhood: nul(nb),
          city: city.trim(),
          state: uf,
          postal_code: nul(cep),
        }
        if (staff && tz.trim()) body.timezone = tz.trim()
        void s.submit(body)
      }}
    >
      <div className="grid grid-cols-1 gap-5 md:grid-cols-2 xl:grid-cols-3">
        <Field invalid={touched && !!errs.postal_code}>
          <FieldLabel>CEP</FieldLabel>
          <Input value={cep} onChange={(e) => setCep(e.target.value)} disabled={disabled} inputMode="numeric" autoComplete="postal-code" />
          <FieldError>{touched ? errs.postal_code : null}</FieldError>
        </Field>
        <Field invalid={touched && !!errs.address} required className="xl:col-span-2">
          <FieldLabel>Logradouro</FieldLabel>
          <Input value={address} onChange={(e) => setAddress(e.target.value)} disabled={disabled} autoComplete="address-line1" />
          <FieldError>{touched ? errs.address : null}</FieldError>
        </Field>
        <Field>
          <FieldLabel>Número</FieldLabel>
          <Input value={number} onChange={(e) => setNumber(e.target.value)} disabled={disabled} />
        </Field>
        <Field>
          <FieldLabel>Complemento</FieldLabel>
          <Input value={complement} onChange={(e) => setComplement(e.target.value)} disabled={disabled} />
        </Field>
        <Field>
          <FieldLabel>Bairro</FieldLabel>
          <Input value={nb} onChange={(e) => setNb(e.target.value)} disabled={disabled} />
        </Field>
        <Field invalid={touched && !!errs.state} required>
          <FieldLabel>UF</FieldLabel>
          <OptionSelect value={uf} onChange={setUf} disabled={disabled} placeholder="Selecione a UF" options={UFS.map((u) => ({ value: u.uf, label: `${u.uf} — ${u.name}` }))} />
          <FieldError>{touched ? errs.state : null}</FieldError>
        </Field>
        <Field invalid={touched && !!errs.city} required>
          <FieldLabel>Cidade</FieldLabel>
          <Input value={city} onChange={(e) => setCity(e.target.value)} disabled={disabled} autoComplete="address-level2" />
          <FieldError>{touched ? errs.city : null}</FieldError>
        </Field>
        {staff ? (
          <Field>
            <FieldLabel>Fuso horário (IANA)</FieldLabel>
            <Input value={tz} onChange={(e) => setTz(e.target.value)} disabled={disabled} placeholder={sugg.data?.timezone ?? 'Sugerido pelo endereço'} />
            <FieldDescription>{sugg.data ? `Sugestão: ${sugg.data.timezone} — ${sugg.data.reason}` : 'Em branco: o servidor sugere pelo endereço na aprovação.'}</FieldDescription>
          </Field>
        ) : null}
      </div>
      <Footer pending={s.pending} general={s.general} label={saveLabel} disabled={disabled} />
    </form>
  )
}

// --------------------------------------------------------------------------- Telas
export function ScreensStepForm({ initial, disabled, onSave, saveLabel }: StepFormProps) {
  const [count, setCount] = React.useState(str(initial?.planned_screens ?? '1'))
  const [tv, setTv] = React.useState<string | null>((initial?.tv_provision as string) ?? 'OWN_TV')
  const [notes, setNotes] = React.useState(str(initial?.notes))
  const [touched, setTouched] = React.useState(false)
  const s = useStepSubmit(onSave)
  const n = Number(count)
  const errs = { planned_screens: !Number.isInteger(n) || n < 1 || n > 500 ? 'Informe de 1 a 500 telas.' : s.serverErrors.planned_screens ?? null }
  return (
    <form
      noValidate
      className="flex flex-col gap-5"
      onSubmit={(e) => {
        e.preventDefault()
        setTouched(true)
        if (errs.planned_screens) return
        void s.submit({ planned_screens: n, tv_provision: tv, notes: nul(notes) })
      }}
    >
      <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
        <Field invalid={touched && !!errs.planned_screens} required>
          <FieldLabel>Quantidade de telas planejadas</FieldLabel>
          <Input value={count} onChange={(e) => setCount(e.target.value.replace(/\D/g, ''))} inputMode="numeric" disabled={disabled} />
          <FieldError>{touched ? errs.planned_screens : null}</FieldError>
        </Field>
        <Field required>
          <FieldLabel>TV</FieldLabel>
          <OptionSelect value={tv} onChange={setTv} disabled={disabled} options={Object.entries(TV_PROVISION_LABEL).map(([value, label]) => ({ value, label }))} />
        </Field>
        <Field className="md:col-span-2">
          <FieldLabel>Observações (locais, tamanho das TVs, internet)</FieldLabel>
          <Textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={3} maxLength={2000} disabled={disabled} />
        </Field>
      </div>
      <Footer pending={s.pending} general={s.general} label={saveLabel} disabled={disabled} />
    </form>
  )
}

// --------------------------------------------------------------------------- Comercial (RD)
export function CommercialStepForm({ initial, disabled, onSave, saveLabel }: StepFormProps) {
  const plans = usePartnerPlans(false)
  const [plan, setPlan] = React.useState<string | null>((initial?.partner_plan_id as string) ?? null)
  const [commission, setCommission] = React.useState(initial?.receives_commission !== false)
  const [pays, setPays] = React.useState(initial?.pays_monthly_fee === true)
  const [fee, setFee] = React.useState(centsToInput((initial?.monthly_fee_cents as number) ?? null))
  const [touched, setTouched] = React.useState(false)
  const s = useStepSubmit(onSave)
  const cents = pays ? parseBrlToCents(fee) : null
  const errs = { fee: pays && (cents === null || Number.isNaN(cents)) ? 'Informe a mensalidade (ex.: 99,90).' : null }
  return (
    <form
      noValidate
      className="flex flex-col gap-5"
      onSubmit={(e) => {
        e.preventDefault()
        setTouched(true)
        if (errs.fee) return
        void s.submit({ partner_plan_id: plan, receives_commission: commission, pays_monthly_fee: pays, monthly_fee_cents: pays ? cents : null })
      }}
    >
      <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
        <Field>
          <FieldLabel>Plano do parceiro</FieldLabel>
          <OptionSelect
            value={plan}
            onChange={setPlan}
            includeNone
            noneLabel="Sem plano"
            disabled={disabled}
            options={(plans.data?.items ?? []).map((p) => ({ value: p.id, label: p.name }))}
          />
        </Field>
        <Field orientation="horizontal" className="min-h-11 md:pt-7">
          <Switch checked={commission} onCheckedChange={setCommission} disabled={disabled} />
          <FieldLabel>Recebe comissão</FieldLabel>
        </Field>
        <Field orientation="horizontal" className="min-h-11">
          <Switch checked={pays} onCheckedChange={setPays} disabled={disabled} />
          <FieldLabel>Paga mensalidade</FieldLabel>
        </Field>
        {pays ? (
          <Field invalid={touched && !!errs.fee} required>
            <FieldLabel>Mensalidade (R$)</FieldLabel>
            <Input value={fee} onChange={(e) => setFee(e.target.value)} inputMode="decimal" disabled={disabled} />
            <FieldError>{touched ? errs.fee : null}</FieldError>
          </Field>
        ) : null}
      </div>
      <Footer pending={s.pending} general={s.general} label={saveLabel} disabled={disabled} />
    </form>
  )
}

// --------------------------------------------------------------------------- Técnico / instalação (RD)
export function InstallationStepForm({ initial, disabled, onSave, saveLabel }: StepFormProps) {
  const [tech, setTech] = React.useState<string | null>((initial?.technician_user_id as string) ?? null)
  const [date, setDate] = React.useState(str(initial?.installation_date))
  const s = useStepSubmit(onSave)
  return (
    <form
      noValidate
      className="flex flex-col gap-5"
      onSubmit={(e) => {
        e.preventDefault()
        void s.submit({ technician_user_id: tech, installation_date: nul(date) })
      }}
    >
      <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
        <TechnicianSelect permission="equipment.manage" label="Técnico responsável" value={tech} onChange={setTech} disabled={disabled} />
        <Field>
          <FieldLabel>Data prevista da instalação</FieldLabel>
          <Input type="date" value={date} onChange={(e) => setDate(e.target.value)} disabled={disabled} />
        </Field>
      </div>
      <Footer pending={s.pending} general={s.general} label={saveLabel} disabled={disabled} />
    </form>
  )
}
