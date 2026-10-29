/**
 * Convidar usuário — POST /auth/invites {email, role, scope_type, scope_id|scope_value, name}.
 * O convite vale 7 dias e chega por e-mail. Escalonamento → 403 ROLE_ESCALATION (motivo exibido).
 */
import * as React from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Field, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { http } from '@/lib/http'
import { formatDateTime } from '@/lib/format'
import { RoleScopeFields } from './RoleScopeFields'
import { EMPTY_ROLE_SCOPE, roleScopePayload, type RoleScopeValue } from './roleScope'
import { explainUserError, type ExplainedError } from './userErrors'
import type { Invite } from '../types'

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

export function InviteDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient()
  const [email, setEmail] = React.useState('')
  const [name, setName] = React.useState('')
  const [rs, setRs] = React.useState<RoleScopeValue>(EMPTY_ROLE_SCOPE)
  const [submitted, setSubmitted] = React.useState(false)
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<ExplainedError | null>(null)

  React.useEffect(() => {
    if (!open) {
      setEmail('')
      setName('')
      setRs(EMPTY_ROLE_SCOPE)
      setSubmitted(false)
      setError(null)
      setPending(false)
    }
  }, [open])

  const emailProblem = !EMAIL_RE.test(email.trim()) ? 'Informe um e-mail válido.' : null
  const { body, problem } = roleScopePayload(rs)

  const submit = async () => {
    setSubmitted(true)
    setError(null)
    if (emailProblem || !body) return
    setPending(true)
    try {
      const inv = await http.post<Invite>('/auth/invites', { ...body, email: email.trim(), name: name.trim() || undefined })
      await qc.invalidateQueries({ queryKey: ['users'] })
      toast.success(`Convite enviado para ${inv.email} (válido até ${formatDateTime(inv.expires_at)}).`)
      onOpenChange(false)
    } catch (e) {
      setError(explainUserError(e))
    } finally {
      setPending(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => (!pending ? onOpenChange(o) : undefined)}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>Convidar usuário</DialogTitle>
          <DialogDescription>A pessoa recebe um link por e-mail (válido por 7 dias) para criar o acesso com o perfil e o escopo escolhidos.</DialogDescription>
        </DialogHeader>
        <form
          noValidate
          className="flex flex-col gap-5"
          onSubmit={(e) => {
            e.preventDefault()
            void submit()
          }}
        >
          <FieldGroup>
            <Field required invalid={submitted && !!emailProblem}>
              <FieldLabel>E-mail</FieldLabel>
              <Input type="email" autoComplete="off" value={email} onChange={(e) => setEmail(e.target.value)} />
              <FieldError>{submitted ? emailProblem : undefined}</FieldError>
            </Field>
            <Field>
              <FieldLabel>Nome (opcional)</FieldLabel>
              <Input value={name} onChange={(e) => setName(e.target.value)} />
            </Field>
            <RoleScopeFields value={rs} onChange={setRs} problem={submitted ? problem : null} />
          </FieldGroup>
          {error ? (
            <Alert variant="danger">
              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <AlertTitle>{error.title}</AlertTitle>
                <AlertDescription className="text-text">
                  {error.detail}
                  {error.code ? <span className="mt-1 block text-xs text-text-muted">Código: {error.code}</span> : null}
                </AlertDescription>
              </div>
            </Alert>
          ) : null}
          <DialogFooter>
            <Button variant="secondary" onClick={() => onOpenChange(false)} disabled={pending}>
              Cancelar
            </Button>
            <Button type="submit" loading={pending}>
              Enviar convite
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
