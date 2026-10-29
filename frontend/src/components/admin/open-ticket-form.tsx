/**
 * Formulário reutilizável "abrir chamado" (POST /support/tickets) — também a partir de um
 * código de anúncio RD-XXXXXX visto na TV (pedido de retirada do estabelecimento).
 * - Estabelecimento: informa só o CÓDIGO; nunca vê nem escolhe o anunciante.
 * - Anunciante: chamado da própria empresa (contexto ativo).
 * - Equipe RD: pode vincular empresa/estabelecimento e usar prioridade Crítica.
 * Valida o formato do código (alfabeto seguro) antes de enviar; o servidor revalida.
 */
import * as React from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { http, type Paginated } from '@/lib/http'
import { errorMessage, isApiError } from '@/lib/errors'
import type { RouteContext } from '@/stores/contextStore'
import { TICKET_CATEGORY, TICKET_PRIORITY } from './labels'
import { EstablishmentPicker, OrgPicker, type PickedEntity } from './pickers'
import { CREATIVE_CODE_RE, normalizeCreativeCode } from './support-links'

const CATEGORIES_BY_CONTEXT: Record<RouteContext, string[]> = {
  rd: Object.keys(TICKET_CATEGORY),
  establishment: ['RETIRADA_ANUNCIO', 'PROBLEMA_TELA', 'MANUTENCAO', 'DUVIDA', 'FINANCEIRO', 'OUTRO'],
  advertiser: ['CAMPANHA', 'DUVIDA', 'FINANCEIRO', 'OUTRO'],
}

export interface OpenTicketInitial {
  category?: string | null
  creativeCode?: string | null
  screenId?: string | null
  establishmentId?: string | null
}

export interface CreatedTicket {
  id: string
  number: string
}

interface ScreenRow {
  id: string
  name: string
  friendly_id: string
  establishment_id: string
}

export interface OpenTicketFormProps {
  context: RouteContext
  /** Organização do contexto (estabelecimento/anunciante). */
  orgId?: string | null
  initial?: OpenTicketInitial
  onCreated: (t: CreatedTicket) => void
  onCancel?: () => void
}

export function OpenTicketForm({ context, orgId, initial, onCreated, onCancel }: OpenTicketFormProps) {
  const categories = CATEGORIES_BY_CONTEXT[context]
  const initialCategory = initial?.category && categories.includes(initial.category) ? initial.category : categories[0]
  const [category, setCategory] = React.useState(initialCategory)
  const [priority, setPriority] = React.useState('MEDIA')
  const [code, setCode] = React.useState(normalizeCreativeCode(initial?.creativeCode) ?? '')
  const [titleInput, setTitleInput] = React.useState<string | null>(null)
  const [description, setDescription] = React.useState('')
  const [org, setOrg] = React.useState<PickedEntity | null>(null)
  const [est, setEst] = React.useState<PickedEntity | null>(initial?.establishmentId ? { id: initial.establishmentId, label: 'Estabelecimento informado' } : null)
  const [screenId, setScreenId] = React.useState<string>(initial?.screenId ?? '')
  const [submitted, setSubmitted] = React.useState(false)
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)

  const staff = context === 'rd'
  const showPlace = context === 'establishment' || staff

  // Tela informada por link (ex.: "Pedir retirada" em Minhas telas): mostra a identificação.
  const preScreen = useQuery({
    queryKey: ['screens', 'detail', initial?.screenId ?? ''],
    queryFn: () => http.get<ScreenRow>(`/screens/${initial?.screenId}`),
    enabled: !!initial?.screenId,
    retry: false,
  })
  const screens = useQuery({
    queryKey: ['screens', 'list', { establishment_id: est?.id, picker: true }],
    queryFn: () => http.get<Paginated<ScreenRow>>('/screens', { query: { establishment_id: est?.id, page_size: 100 } }),
    enabled: showPlace && !!est?.id && !initial?.screenId,
  })

  const normalized = normalizeCreativeCode(code)
  // Título sugerido para pedidos de retirada até a pessoa editar o campo.
  const title = titleInput ?? (category === 'RETIRADA_ANUNCIO' && normalized ? `Retirada do anúncio ${normalized}` : '')
  const setTitle = (v: string) => setTitleInput(v)
  const codeRequired = category === 'RETIRADA_ANUNCIO'
  const codeProblem = normalized
    ? CREATIVE_CODE_RE.test(normalized)
      ? null
      : 'Código inválido. Use o formato RD-XXXXXX exibido na tela (letras e números, sem O, I, L, K, W, X, 0 e 1).'
    : codeRequired
      ? 'Informe o código do anúncio exibido na tela (RD-XXXXXX).'
      : null
  const titleProblem = title.trim().length < 3 ? 'Informe um título (mínimo 3 caracteres).' : null
  const descProblem = description.trim().length < 3 ? 'Descreva o que aconteceu.' : null
  const orgProblem = !staff && !orgId ? 'Contexto sem empresa: troque para “Minha unidade” ou “Minhas campanhas”.' : null

  const submit = async () => {
    setSubmitted(true)
    setError(null)
    if (codeProblem || titleProblem || descProblem || orgProblem) return
    setPending(true)
    try {
      const body: Record<string, unknown> = {
        category,
        priority,
        title: title.trim(),
        description: description.trim(),
        creative_code: normalized ?? undefined,
        org_id: staff ? org?.id : orgId,
        establishment_id: showPlace ? (est?.id ?? undefined) : undefined,
        screen_id: showPlace ? screenId || undefined : undefined,
      }
      const t = await http.post<CreatedTicket>('/support/tickets', body)
      onCreated(t)
    } catch (e) {
      if (isApiError(e) && e.code === 'CREATIVE_CODE_INVALID') setError('O servidor não reconheceu o formato do código. Confira o RD-XXXXXX exibido na tela.')
      else if (isApiError(e) && e.status === 404) setError('Empresa, estabelecimento ou tela fora do seu acesso. Revise os dados do chamado.')
      else setError(errorMessage(e))
    } finally {
      setPending(false)
    }
  }

  return (
    <form
      noValidate
      className="flex flex-col gap-6"
      onSubmit={(e) => {
        e.preventDefault()
        void submit()
      }}
    >
      {orgProblem ? (
        <Alert variant="warning">
          <AlertDescription className="text-text">{orgProblem}</AlertDescription>
        </Alert>
      ) : null}
      <FieldGroup>
        <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
          <Field required>
            <FieldLabel>Assunto</FieldLabel>
            <Select value={category} onValueChange={setCategory}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  {categories.map((c) => (
                    <SelectItem key={c} value={c}>
                      {TICKET_CATEGORY[c]}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
          </Field>
          <Field>
            <FieldLabel>Prioridade</FieldLabel>
            <Select value={priority} onValueChange={setPriority}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  {(staff ? ['BAIXA', 'MEDIA', 'ALTA', 'CRITICA'] : ['BAIXA', 'MEDIA', 'ALTA']).map((p) => (
                    <SelectItem key={p} value={p}>
                      {TICKET_PRIORITY[p].label}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
          </Field>
        </div>
        <Field required={codeRequired} invalid={submitted && !!codeProblem}>
          <FieldLabel>Código do anúncio (ID exibido na tela)</FieldLabel>
          <Input
            value={code}
            onChange={(e) => setCode(e.target.value.toUpperCase())}
            placeholder="RD-XXXXXX"
            autoComplete="off"
            spellCheck={false}
            className="max-w-xs font-mono"
          />
          <FieldDescription>
            {context === 'establishment'
              ? 'Informe só o código que aparece no canto do anúncio. A RD identifica a campanha; os dados do anunciante não são compartilhados.'
              : 'Opcional, exceto para pedidos de retirada de anúncio.'}
          </FieldDescription>
          <FieldError>{submitted ? codeProblem : undefined}</FieldError>
        </Field>
        {staff ? (
          <OrgPicker
            label="Empresa (opcional)"
            value={org}
            onChange={(o) => {
              setOrg(o)
              setEst(null)
              setScreenId('')
            }}
          />
        ) : null}
        {showPlace ? (
          initial?.screenId ? (
            <Field>
              <FieldLabel>Tela</FieldLabel>
              <p className="m-0 text-sm text-text">
                {preScreen.data ? `${preScreen.data.friendly_id} — ${preScreen.data.name}` : preScreen.isLoading ? 'Carregando…' : 'Tela informada pelo link'}
              </p>
            </Field>
          ) : (
            <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
              <EstablishmentPicker
                label={staff ? 'Estabelecimento (opcional)' : 'Estabelecimento'}
                orgId={staff ? org?.id : orgId}
                value={est}
                onChange={(e) => {
                  setEst(e)
                  setScreenId('')
                }}
              />
              {est ? (
                <Field>
                  <FieldLabel>Tela (opcional)</FieldLabel>
                  <Select value={screenId || 'none'} onValueChange={(v) => setScreenId(v === 'none' ? '' : v)}>
                    <SelectTrigger>
                      <SelectValue placeholder={screens.isLoading ? 'Carregando…' : 'Selecione'} />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectGroup>
                        <SelectItem value="none">Nenhuma tela específica</SelectItem>
                        {(screens.data?.items ?? []).map((s) => (
                          <SelectItem key={s.id} value={s.id}>
                            {s.friendly_id} — {s.name}
                          </SelectItem>
                        ))}
                      </SelectGroup>
                    </SelectContent>
                  </Select>
                </Field>
              ) : null}
            </div>
          )
        ) : null}
        <Field required invalid={submitted && !!titleProblem}>
          <FieldLabel>Título</FieldLabel>
          <Input value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} />
          <FieldError>{submitted ? titleProblem : undefined}</FieldError>
        </Field>
        <Field required invalid={submitted && !!descProblem}>
          <FieldLabel>Descrição</FieldLabel>
          <Textarea value={description} maxLength={10000} onChange={(e) => setDescription(e.target.value)} className="min-h-32" />
          <FieldDescription>
            {category === 'RETIRADA_ANUNCIO' ? 'Explique o motivo do pedido de retirada (ex.: conteúdo inadequado ao local).' : 'Inclua o que você viu, quando e em qual tela.'}
          </FieldDescription>
          <FieldError>{submitted ? descProblem : undefined}</FieldError>
        </Field>
      </FieldGroup>
      {error ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">{error}</AlertDescription>
        </Alert>
      ) : null}
      <div className="flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
        {onCancel ? (
          <Button variant="secondary" onClick={onCancel} disabled={pending}>
            Cancelar
          </Button>
        ) : null}
        <Button type="submit" loading={pending} disabled={!!orgProblem}>
          Abrir chamado
        </Button>
      </div>
    </form>
  )
}
