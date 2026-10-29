/**
 * Minha conta: dados do perfil (GET /me), edição do TELEFONE do próprio usuário (PATCH /me `{phone}`) e
 * preferência de tema (persistente; sincronizada via PATCH /me).
 *
 * O telefone do usuário é o contato do pagador PIX quando a empresa não tem telefone (422
 * `PAYER_DATA_INCOMPLETE` missing=[phone]); o checkout traz `?voltar=<caminho interno>` e, depois de
 * salvar, a pessoa volta para onde estava (carrinho com seleção e cupom no endereço).
 */
import * as React from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Monitor, Moon, Pencil, ShieldCheck, Smartphone, Sun } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { useAuthStore } from '@/stores/authStore'
import { isThemePreference, useThemeStore } from '@/stores/themeStore'
import { EMPTY_VALUE } from '@/lib/format'
import { http } from '@/lib/http'
import { errorMessage } from '@/lib/errors'
import { qk } from '@/lib/queryKeys'
import { displayPhone, maskPhone, nationalDigits, nextMaskedPhone, onlyDigits, phoneProblem } from '@/lib/phone'
import type { MeUser } from '@/features/auth/types'

/** Só caminhos internos (evita redirecionamento aberto). */
function safeReturnPath(raw: string | null): string | null {
  if (!raw || !raw.startsWith('/') || raw.startsWith('//') || raw.startsWith('/\\')) return null
  return raw
}

function PhoneEditor({ current, initiallyOpen, onSaved }: { current: string | null | undefined; initiallyOpen: boolean; onSaved: () => void }) {
  const qc = useQueryClient()
  const [editing, setEditing] = React.useState(initiallyOpen)
  const [value, setValue] = React.useState(() => maskPhone(nationalDigits(current)))
  const [submitted, setSubmitted] = React.useState(false)
  const [serverError, setServerError] = React.useState<string | null>(null)

  const save = useMutation({
    mutationFn: (digits: string) => http.patch<{ user?: Partial<MeUser> }>('/me', { phone: digits }),
    onSuccess: (res, digits) => {
      const phone = res?.user?.phone ?? `55${digits}`
      useAuthStore.setState((s) => (s.me ? { me: { ...s.me, user: { ...s.me.user, phone } } } : s))
      void qc.invalidateQueries({ queryKey: qk.me })
      setServerError(null)
      setSubmitted(false)
      setEditing(false)
      toast.success('Telefone salvo.')
      onSaved()
    },
    onError: (e) => setServerError(errorMessage(e)),
  })

  const problem = submitted ? phoneProblem(value) : null
  const shownError = problem ?? serverError

  if (!editing) {
    return (
      <Button variant="tertiary" className="self-start" onClick={() => setEditing(true)}>
        <Pencil aria-hidden="true" />
        {current ? 'Alterar telefone' : 'Adicionar telefone'}
      </Button>
    )
  }

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    setSubmitted(true)
    setServerError(null)
    if (phoneProblem(value)) return
    save.mutate(onlyDigits(value))
  }

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-3" aria-label="Editar telefone">
      <Field invalid={!!shownError} className="max-w-xs">
        <FieldLabel>Telefone (com DDD)</FieldLabel>
        <Input
          value={value}
          onChange={(e) => {
            setValue(nextMaskedPhone(e.target.value, value))
            setServerError(null)
          }}
          inputMode="tel"
          autoComplete="tel-national"
          placeholder="(11) 91234-5678"
          maxLength={16}
          autoFocus={initiallyOpen}
        />
        <FieldDescription>Fixo ou celular. Também é o contato das cobranças PIX quando a empresa não tem telefone cadastrado.</FieldDescription>
        <FieldError>{shownError}</FieldError>
      </Field>
      <div className="flex flex-wrap gap-2">
        <Button type="submit" loading={save.isPending} loadingText="Salvando…">
          Salvar telefone
        </Button>
        <Button
          type="button"
          variant="secondary"
          disabled={save.isPending}
          onClick={() => {
            setEditing(false)
            setSubmitted(false)
            setServerError(null)
            setValue(maskPhone(nationalDigits(current)))
          }}
        >
          Cancelar
        </Button>
      </div>
    </form>
  )
}

export default function AccountPage() {
  const me = useAuthStore((s) => s.me)
  const preference = useThemeStore((s) => s.preference)
  const setPreference = useThemeStore((s) => s.setPreference)
  const navigate = useNavigate()
  const [sp] = useSearchParams()
  const returnTo = safeReturnPath(sp.get('voltar'))

  return (
    <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
      <PageHeader title="Minha conta" />
      <Card>
        <CardHeader>
          <CardTitle>Perfil</CardTitle>
        </CardHeader>
        {returnTo ? (
          <Alert variant="info" data-testid="account-return-notice">
            <div>
              <AlertTitle>Complete seus dados para pagar</AlertTitle>
              <AlertDescription>
                <p className="m-0">
                  Informe seu telefone com DDD. Depois de salvar, você volta automaticamente ao pagamento — nada do que você
                  preencheu lá foi perdido. CPF/CNPJ e dados da empresa são atualizados pelo suporte.
                </p>
                <Link className="mt-1 inline-block font-semibold text-link underline-offset-4 hover:underline" to={returnTo}>
                  Voltar ao pagamento sem salvar
                </Link>
              </AlertDescription>
            </div>
          </Alert>
        ) : null}
        <dl className="m-0 grid grid-cols-1 gap-4 text-sm sm:grid-cols-2">
          <div>
            <dt className="text-xs text-text-muted">Nome</dt>
            <dd className="m-0 font-semibold text-text">{me?.user.name ?? EMPTY_VALUE}</dd>
          </div>
          <div>
            <dt className="text-xs text-text-muted">E-mail</dt>
            <dd className="m-0 break-all font-semibold text-text">{me?.user.email ?? EMPTY_VALUE}</dd>
          </div>
          <div className="flex flex-col gap-1 sm:col-span-2">
            <dt className="text-xs text-text-muted">Telefone</dt>
            <dd className="m-0 flex flex-col gap-2">
              <span className="font-semibold text-text" data-testid="account-phone">
                {displayPhone(me?.user.phone)}
              </span>
              {me ? (
                <PhoneEditor
                  current={me.user.phone}
                  initiallyOpen={!!returnTo && !me.user.phone}
                  onSaved={() => {
                    if (returnTo) navigate(returnTo)
                  }}
                />
              ) : null}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-text-muted">Verificação em duas etapas</dt>
            <dd className="m-0">
              {me?.user.mfa_enabled ? <Badge tone="success">Ativada</Badge> : <Badge tone="warning">Desativada</Badge>}
            </dd>
          </div>
        </dl>
        {me?.assignments?.length ? (
          <div className="flex flex-col gap-2 border-t border-border pt-4">
            <h3 className="text-title-card text-text">Acessos</h3>
            <ul className="m-0 flex list-none flex-col gap-1 p-0 text-sm">
              {me.assignments.map((a, i) => (
                <li key={`${a.role}-${a.scope_id ?? a.scope_value ?? i}`} className="text-text">
                  {a.role} <span className="text-text-muted">· {a.scope_label ?? a.scope_type}</span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        <div className="flex flex-wrap gap-2 border-t border-border pt-4">
          <Button variant="secondary" asChild>
            <Link to="/minha-conta/seguranca">
              <ShieldCheck aria-hidden="true" />
              Segurança
            </Link>
          </Button>
          <Button variant="secondary" asChild>
            <Link to="/minha-conta/sessoes">
              <Smartphone aria-hidden="true" />
              Sessões ativas
            </Link>
          </Button>
        </div>
      </Card>

      <Card>
        <CardHeader>
          <div className="flex flex-col gap-1">
            <CardTitle id="tema-titulo">Tema da interface</CardTitle>
            <CardDescription>A escolha fica salva neste navegador e no seu perfil.</CardDescription>
          </div>
        </CardHeader>
        <ToggleGroup
          type="single"
          value={preference}
          onValueChange={(v) => isThemePreference(v) && setPreference(v)}
          aria-labelledby="tema-titulo"
          className="self-start"
        >
          <ToggleGroupItem value="dark">
            <Moon aria-hidden="true" />
            Escuro
          </ToggleGroupItem>
          <ToggleGroupItem value="light">
            <Sun aria-hidden="true" />
            Claro
          </ToggleGroupItem>
          <ToggleGroupItem value="system">
            <Monitor aria-hidden="true" />
            Sistema
          </ToggleGroupItem>
        </ToggleGroup>
      </Card>
    </div>
  )
}
