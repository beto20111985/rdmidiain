/**
 * Formulário de empresa (criação e edição): PF/PJ com CPF/CNPJ validado no cliente
 * (inclusive CNPJ alfanumérico), papéis explícitos combináveis, contatos e endereço.
 * ANUNCIANTE (quem paga as campanhas): e-mail e telefone com DDD obrigatórios — os mesmos dados que o
 * pagamento exige do pagador (AÇÃO 004; servidor: ADVERTISER_PAYER_DATA_REQUIRED).
 * O servidor revalida tudo (DOCUMENT_INVALID, DOCUMENT_ALREADY_REGISTERED…).
 */
import * as React from 'react'
import { Controller, useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Checkbox } from '@/components/ui/checkbox'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel, FieldLegend, FieldSet } from '@/components/ui/field'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Separator } from '@/components/ui/separator'
import { BUSINESS_ROLES, BUSINESS_ROLE_LABELS, UFS, type BusinessRole } from '@/components/admin/labels'
import { documentProblem, formatDocument, stripDocument } from '@/components/admin/documents'
import { errorMessage, isApiError } from '@/lib/errors'
import { maskPhone, nationalDigits, nextMaskedPhone, phoneProblem } from '@/lib/phone'
import type { Organization } from '../types'

const optionalText = (max: number) => z.string().trim().max(max, `Máximo de ${max} caracteres.`)

export const ADVERTISER_PHONE_REQUIRED = 'Telefone com DDD obrigatório para anunciante (exigido no pagamento).'
export const ADVERTISER_EMAIL_REQUIRED = 'E-mail obrigatório para anunciante (exigido no pagamento).'

const schema = z
  .object({
    document_type: z.enum(['CPF', 'CNPJ']),
    document: z.string(),
    name: z.string().trim().min(2, 'Informe o nome (mínimo 2 caracteres).').max(255),
    trade_name: optionalText(255),
    business_roles: z.array(z.enum(BUSINESS_ROLES)).min(1, 'Escolha ao menos um papel.'),
    responsible_name: optionalText(255),
    email: z.union([z.literal(''), z.string().trim().email('E-mail inválido.')]),
    phone: optionalText(30),
    postal_code: z.string().trim().refine((v) => v === '' || /^\d{8}$/.test(v.replace(/\D/g, '')), 'CEP com 8 dígitos.'),
    address: optionalText(255),
    number: optionalText(20),
    complement: optionalText(100),
    neighborhood: optionalText(100),
    city: optionalText(100),
    state: z.string(),
    notes: optionalText(4000),
  })
  .superRefine((v, ctx) => {
    const p = documentProblem(v.document, v.document_type)
    if (p) ctx.addIssue({ code: 'custom', path: ['document'], message: p })
    // Anunciante paga as campanhas: o cadastro precisa dos dados do pagador (e-mail + telefone com DDD)
    if (v.business_roles.includes('ADVERTISER')) {
      if (!v.phone.trim()) ctx.addIssue({ code: 'custom', path: ['phone'], message: ADVERTISER_PHONE_REQUIRED })
      else {
        const ph = phoneProblem(v.phone)
        if (ph) ctx.addIssue({ code: 'custom', path: ['phone'], message: ph })
      }
      if (!v.email.trim()) ctx.addIssue({ code: 'custom', path: ['email'], message: ADVERTISER_EMAIL_REQUIRED })
    }
  })

export type OrganizationFormValues = z.infer<typeof schema>

/** Telefone salvo → máscara (DDD + número) quando reconhecível; senão, o texto como está. */
function initialPhone(stored: string | null | undefined): string {
  if (!stored) return ''
  const d = nationalDigits(stored)
  return d.length === 10 || d.length === 11 ? maskPhone(d) : stored
}

function toValues(o?: Organization | null): OrganizationFormValues {
  return {
    document_type: o?.document_type ?? 'CNPJ',
    document: o ? formatDocument(o.document, o.document_type) : '',
    name: o?.name ?? '',
    trade_name: o?.trade_name ?? '',
    business_roles: (o?.business_roles ?? []).filter((r): r is BusinessRole => (BUSINESS_ROLES as readonly string[]).includes(r)),
    responsible_name: o?.responsible_name ?? '',
    email: o?.email ?? '',
    phone: initialPhone(o?.phone),
    postal_code: o?.postal_code ?? '',
    address: o?.address ?? '',
    number: o?.number ?? '',
    complement: o?.complement ?? '',
    neighborhood: o?.neighborhood ?? '',
    city: o?.city ?? '',
    state: o?.state ?? '',
    notes: o?.notes ?? '',
  }
}

/** Converte o formulário no corpo da API (somente campos preenchidos; na edição, só os alterados). */
function toPayload(v: OrganizationFormValues, dirty?: Partial<Record<keyof OrganizationFormValues, unknown>>) {
  const out: Record<string, unknown> = {}
  const include = (k: keyof OrganizationFormValues) => !dirty || Boolean(dirty[k])
  const text = (k: keyof OrganizationFormValues, value: string) => {
    if (include(k) && value.trim() !== '') out[k] = value.trim()
  }
  if (include('document') || include('document_type')) {
    out.document = stripDocument(v.document)
    out.document_type = v.document_type
  }
  if (include('business_roles')) out.business_roles = v.business_roles
  text('name', v.name)
  text('trade_name', v.trade_name)
  text('responsible_name', v.responsible_name)
  text('email', v.email)
  text('phone', v.phone)
  if (include('postal_code') && v.postal_code.trim()) out.postal_code = v.postal_code.replace(/\D/g, '')
  text('address', v.address)
  text('number', v.number)
  text('complement', v.complement)
  text('neighborhood', v.neighborhood)
  text('city', v.city)
  if (include('state') && v.state) out.state = v.state
  text('notes', v.notes)
  return out
}

const FIELD_CODES: Record<string, keyof OrganizationFormValues> = {
  DOCUMENT_INVALID: 'document',
  DOCUMENT_ALREADY_REGISTERED: 'document',
  BUSINESS_ROLE_INVALID: 'business_roles',
  BUSINESS_ROLE_REQUIRED: 'business_roles',
}

export interface OrganizationFormProps {
  initial?: Organization | null
  submitLabel: string
  onSubmit: (payload: Record<string, unknown>) => Promise<unknown>
  onCancel?: () => void
  disabled?: boolean
}

export function OrganizationForm({ initial, submitLabel, onSubmit, onCancel, disabled }: OrganizationFormProps) {
  const [serverError, setServerError] = React.useState<string | null>(null)
  const {
    register,
    control,
    handleSubmit,
    setError,
    watch,
    reset,
    formState: { errors, isSubmitting, dirtyFields, isDirty },
  } = useForm<OrganizationFormValues>({ resolver: zodResolver(schema), defaultValues: toValues(initial) })

  React.useEffect(() => {
    reset(toValues(initial))
  }, [initial, reset])

  const docType = watch('document_type')
  const isAdvertiser = watch('business_roles').includes('ADVERTISER')
  const editing = Boolean(initial)

  const submit = async (v: OrganizationFormValues) => {
    setServerError(null)
    const payload = toPayload(v, editing ? (dirtyFields as Record<string, unknown>) : undefined)
    if (editing && Object.keys(payload).length === 0) {
      setServerError('Nenhuma alteração para salvar.')
      return
    }
    try {
      await onSubmit(payload)
    } catch (e) {
      if (isApiError(e)) {
        const fe = e.fieldErrors
        let mapped = false
        for (const [k, msg] of Object.entries(fe)) {
          if (k in v) {
            setError(k as keyof OrganizationFormValues, { message: msg })
            mapped = true
          }
        }
        const field = FIELD_CODES[e.code]
        if (field) {
          setError(field, { message: e.message })
          mapped = true
        }
        if (!mapped) setServerError(e.message)
      } else setServerError(errorMessage(e))
    }
  }

  return (
    <form noValidate onSubmit={handleSubmit(submit)} className="flex flex-col gap-6">
      {serverError ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">{serverError}</AlertDescription>
        </Alert>
      ) : null}
      <fieldset disabled={disabled} className="m-0 flex min-w-0 flex-col gap-6 border-0 p-0">
        <FieldGroup>
          <Field>
            <FieldLabel id="org-person-type">Tipo de pessoa</FieldLabel>
            <Controller
              control={control}
              name="document_type"
              render={({ field }) => (
                <ToggleGroup
                  type="single"
                  aria-labelledby="org-person-type"
                  value={field.value}
                  onValueChange={(val) => val && field.onChange(val)}
                  className="w-fit"
                >
                  <ToggleGroupItem value="CNPJ">Pessoa jurídica (CNPJ)</ToggleGroupItem>
                  <ToggleGroupItem value="CPF">Pessoa física (CPF)</ToggleGroupItem>
                </ToggleGroup>
              )}
            />
          </Field>
          <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
            <Field invalid={!!errors.document} required>
              <FieldLabel>{docType === 'CPF' ? 'CPF' : 'CNPJ'}</FieldLabel>
              <Input autoComplete="off" spellCheck={false} {...register('document')} />
              <FieldDescription>
                {docType === 'CNPJ' ? 'Aceita o CNPJ alfanumérico (letras nas 12 primeiras posições).' : 'Somente números ou com máscara.'}
              </FieldDescription>
              <FieldError>{errors.document?.message}</FieldError>
            </Field>
            <Field invalid={!!errors.name} required>
              <FieldLabel>{docType === 'CPF' ? 'Nome completo' : 'Razão social'}</FieldLabel>
              <Input autoComplete="organization" {...register('name')} />
              <FieldError>{errors.name?.message}</FieldError>
            </Field>
            <Field invalid={!!errors.trade_name}>
              <FieldLabel>Nome fantasia</FieldLabel>
              <Input {...register('trade_name')} />
              <FieldError>{errors.trade_name?.message}</FieldError>
            </Field>
          </div>
          <Controller
            control={control}
            name="business_roles"
            render={({ field }) => (
              <FieldSet aria-invalid={!!errors.business_roles || undefined}>
                <FieldLegend>Papéis da empresa (combináveis) *</FieldLegend>
                <div className="grid grid-cols-1 gap-x-6 sm:grid-cols-2">
                  {BUSINESS_ROLES.map((r) => (
                    <Field key={r} orientation="horizontal" className="min-h-11">
                      <Checkbox
                        checked={field.value.includes(r)}
                        onCheckedChange={(c) =>
                          field.onChange(c ? [...field.value, r] : field.value.filter((x: string) => x !== r))
                        }
                      />
                      <FieldLabel className="font-normal">{BUSINESS_ROLE_LABELS[r]}</FieldLabel>
                    </Field>
                  ))}
                </div>
                {errors.business_roles ? (
                  <p role="alert" className="text-xs font-medium text-danger">
                    {errors.business_roles.message}
                  </p>
                ) : null}
              </FieldSet>
            )}
          />
        </FieldGroup>

        <Separator />
        <FieldGroup>
          <h2 className="text-title-card text-text">Contatos</h2>
          <div className="grid grid-cols-1 gap-5 md:grid-cols-3">
            <Field invalid={!!errors.responsible_name}>
              <FieldLabel>Responsável</FieldLabel>
              <Input autoComplete="name" {...register('responsible_name')} />
              <FieldError>{errors.responsible_name?.message}</FieldError>
            </Field>
            <Field invalid={!!errors.email} required={isAdvertiser}>
              <FieldLabel>E-mail</FieldLabel>
              <Input type="email" autoComplete="email" {...register('email')} />
              {isAdvertiser ? <FieldDescription>Obrigatório para anunciante: contato do pagador nas cobranças.</FieldDescription> : null}
              <FieldError>{errors.email?.message}</FieldError>
            </Field>
            <Field invalid={!!errors.phone} required={isAdvertiser}>
              <FieldLabel>Telefone</FieldLabel>
              <Controller
                control={control}
                name="phone"
                render={({ field }) => (
                  <Input
                    type="tel"
                    inputMode="tel"
                    autoComplete="tel-national"
                    placeholder="(11) 91234-5678"
                    name={field.name}
                    ref={field.ref}
                    value={field.value}
                    onBlur={field.onBlur}
                    onChange={(e) => field.onChange(nextMaskedPhone(e.target.value, field.value))}
                  />
                )}
              />
              {isAdvertiser ? <FieldDescription>Obrigatório para anunciante, com DDD (exigido no pagamento).</FieldDescription> : null}
              <FieldError>{errors.phone?.message}</FieldError>
            </Field>
          </div>
        </FieldGroup>

        <Separator />
        <FieldGroup>
          <h2 className="text-title-card text-text">Endereço</h2>
          <div className="grid grid-cols-1 gap-5 md:grid-cols-6">
            <Field invalid={!!errors.postal_code} className="md:col-span-2">
              <FieldLabel>CEP</FieldLabel>
              <Input inputMode="numeric" autoComplete="postal-code" {...register('postal_code')} />
              <FieldError>{errors.postal_code?.message}</FieldError>
            </Field>
            <Field className="md:col-span-4">
              <FieldLabel>Logradouro</FieldLabel>
              <Input autoComplete="address-line1" {...register('address')} />
            </Field>
            <Field className="md:col-span-1">
              <FieldLabel>Número</FieldLabel>
              <Input {...register('number')} />
            </Field>
            <Field className="md:col-span-2">
              <FieldLabel>Complemento</FieldLabel>
              <Input autoComplete="address-line2" {...register('complement')} />
            </Field>
            <Field className="md:col-span-3">
              <FieldLabel>Bairro</FieldLabel>
              <Input {...register('neighborhood')} />
            </Field>
            <Field className="md:col-span-4">
              <FieldLabel>Cidade</FieldLabel>
              <Input autoComplete="address-level2" {...register('city')} />
            </Field>
            <Field className="md:col-span-2">
              <FieldLabel>UF</FieldLabel>
              <Controller
                control={control}
                name="state"
                render={({ field }) => (
                  <Select value={field.value} onValueChange={field.onChange}>
                    <SelectTrigger>
                      <SelectValue placeholder="Selecione" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectGroup>
                        {UFS.map((uf) => (
                          <SelectItem key={uf} value={uf}>
                            {uf}
                          </SelectItem>
                        ))}
                      </SelectGroup>
                    </SelectContent>
                  </Select>
                )}
              />
            </Field>
          </div>
          <Field invalid={!!errors.notes}>
            <FieldLabel>Observações internas</FieldLabel>
            <Textarea {...register('notes')} />
            <FieldError>{errors.notes?.message}</FieldError>
          </Field>
        </FieldGroup>
      </fieldset>
      <div className="flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
        {onCancel ? (
          <Button variant="secondary" onClick={onCancel} disabled={isSubmitting}>
            Cancelar
          </Button>
        ) : null}
        <Button type="submit" loading={isSubmitting} disabled={disabled || (editing && !isDirty)}>
          {submitLabel}
        </Button>
      </div>
    </form>
  )
}
