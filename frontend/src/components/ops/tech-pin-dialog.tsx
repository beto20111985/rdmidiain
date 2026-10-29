/**
 * Definir PIN técnico do player (menu técnico/saída do kiosk). O PIN digitado vai ao backend,
 * que gera sal + hash PBKDF2 e envia o comando `set_tech_pin` — o PIN nunca é guardado em claro
 * nem exibido de volta. 6 a 12 dígitos, confirmação e motivo (auditoria).
 */
import * as React from 'react'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { errorMessage } from '@/lib/errors'

export function TechPinDialog({
  open,
  onOpenChange,
  targetLabel,
  onSubmit,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  targetLabel: string
  onSubmit: (pin: string, reason: string) => Promise<unknown>
}) {
  const [pin, setPin] = React.useState('')
  const [pin2, setPin2] = React.useState('')
  const [reason, setReason] = React.useState('')
  const [touched, setTouched] = React.useState(false)
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<unknown>(null)

  React.useEffect(() => {
    if (!open) {
      setPin('')
      setPin2('')
      setReason('')
      setTouched(false)
      setPending(false)
      setError(null)
    }
  }, [open])

  const pinOk = /^\d{6,12}$/.test(pin)
  const matchOk = pin === pin2
  const reasonOk = reason.trim().length >= 3

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setTouched(true)
    if (!pinOk || !matchOk || !reasonOk) return
    setPending(true)
    setError(null)
    try {
      await onSubmit(pin, reason.trim())
      onOpenChange(false)
    } catch (err) {
      setError(err)
    } finally {
      setPending(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => (!pending ? onOpenChange(o) : undefined)}>
      <DialogContent>
        <form onSubmit={submit} noValidate className="flex flex-col gap-5">
          <DialogHeader>
            <DialogTitle>Definir PIN técnico</DialogTitle>
            <DialogDescription>
              Vale para {targetLabel}. O servidor guarda apenas o hash do PIN e o envia ao player como comando auditado.
            </DialogDescription>
          </DialogHeader>
          <Field invalid={touched && !pinOk} required>
            <FieldLabel>Novo PIN</FieldLabel>
            <Input
              type="password"
              inputMode="numeric"
              autoComplete="new-password"
              value={pin}
              onChange={(e) => setPin(e.target.value.replace(/\D/g, '').slice(0, 12))}
            />
            <FieldDescription>De 6 a 12 dígitos. Evite sequências óbvias (123456).</FieldDescription>
            <FieldError>{touched && !pinOk ? 'O PIN deve ter de 6 a 12 dígitos.' : null}</FieldError>
          </Field>
          <Field invalid={touched && !matchOk} required>
            <FieldLabel>Confirme o PIN</FieldLabel>
            <Input
              type="password"
              inputMode="numeric"
              autoComplete="new-password"
              value={pin2}
              onChange={(e) => setPin2(e.target.value.replace(/\D/g, '').slice(0, 12))}
            />
            <FieldError>{touched && !matchOk ? 'Os PINs não conferem.' : null}</FieldError>
          </Field>
          <Field invalid={touched && !reasonOk} required>
            <FieldLabel>Motivo</FieldLabel>
            <Textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={2} maxLength={2000} />
            <FieldError>{touched && !reasonOk ? 'Informe o motivo (mínimo 3 caracteres).' : null}</FieldError>
          </Field>
          {error ? (
            <Alert variant="danger">
              <AlertDescription className="text-text">{errorMessage(error)}</AlertDescription>
            </Alert>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)} disabled={pending}>
              Cancelar
            </Button>
            <Button type="submit" loading={pending}>
              Enviar PIN
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
