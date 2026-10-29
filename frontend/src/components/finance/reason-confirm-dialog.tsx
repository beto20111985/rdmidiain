/**
 * Confirmação destrutiva COM motivo obrigatório (auditoria) e, opcionalmente, texto digitado
 * (DS §10/§17). Erros da ação ficam dentro do diálogo; foco preso; Esc cancela.
 */
import * as React from 'react'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { errorMessage } from '@/lib/errors'

export interface ReasonConfirmDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description?: string
  consequences?: string[]
  reasonLabel?: string
  minReason?: number
  requireText?: string
  confirmLabel: string
  destructive?: boolean
  onConfirm: (reason: string) => Promise<unknown> | unknown
}

export function ReasonConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  consequences,
  reasonLabel = 'Motivo',
  minReason = 5,
  requireText,
  confirmLabel,
  destructive = true,
  onConfirm,
}: ReasonConfirmDialogProps) {
  const [reason, setReason] = React.useState('')
  const [typed, setTyped] = React.useState('')
  const [reasonError, setReasonError] = React.useState<string | null>(null)
  const [error, setError] = React.useState<unknown>(null)
  const [pending, setPending] = React.useState(false)

  React.useEffect(() => {
    if (!open) {
      setReason('')
      setTyped('')
      setReasonError(null)
      setError(null)
      setPending(false)
    }
  }, [open])

  const matches = !requireText || typed.trim() === requireText.trim()
  const confirm = async () => {
    if (reason.trim().length < minReason) {
      setReasonError(`Informe o motivo (mínimo ${minReason} caracteres).`)
      return
    }
    if (!matches || pending) return
    setReasonError(null)
    setError(null)
    setPending(true)
    try {
      await onConfirm(reason.trim())
      onOpenChange(false)
    } catch (e) {
      setError(e)
    } finally {
      setPending(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => (!pending ? onOpenChange(o) : undefined)}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {description ? <DialogDescription>{description}</DialogDescription> : null}
        </DialogHeader>
        {consequences?.length ? (
          <ul className="m-0 flex list-disc flex-col gap-1 pl-5 text-sm text-text">
            {consequences.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
        ) : null}
        <FieldGroup>
          <Field invalid={!!reasonError}>
            <FieldLabel required>{reasonLabel}</FieldLabel>
            <Textarea rows={2} value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} />
            <FieldError>{reasonError}</FieldError>
          </Field>
          {requireText ? (
            <Field>
              <FieldLabel>
                Digite <strong className="font-bold">{requireText}</strong> para confirmar
              </FieldLabel>
              <Input value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" autoCapitalize="off" spellCheck={false} />
              <FieldDescription>Esta confirmação evita alterações acidentais.</FieldDescription>
            </Field>
          ) : null}
        </FieldGroup>
        {error ? (
          <Alert variant="danger">
            <AlertDescription>{errorMessage(error)}</AlertDescription>
          </Alert>
        ) : null}
        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)} disabled={pending}>
            Cancelar
          </Button>
          <Button variant={destructive ? 'destructive' : 'primary'} disabled={!matches} loading={pending} onClick={() => void confirm()}>
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
