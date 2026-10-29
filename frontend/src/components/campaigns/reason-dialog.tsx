/**
 * Diálogo de ação com MOTIVO obrigatório (rejeitar, kill emergencial, revogar, cancelar…).
 * Mínimo de caracteres validado no cliente (o servidor revalida); erro do servidor fica
 * dentro do diálogo; opcionalmente exige digitar o nome para ações destrutivas.
 */
import * as React from 'react'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Textarea } from '@/components/ui/textarea'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { describeError } from '@/pages/campaigns/lib/errorText'

export interface ReasonDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description?: React.ReactNode
  reasonLabel?: string
  minLength?: number
  /** Motivo opcional (ex.: pausar). */
  optional?: boolean
  confirmLabel: string
  destructive?: boolean
  requireText?: string
  children?: React.ReactNode
  onConfirm: (reason: string) => Promise<unknown>
}

export function ReasonDialog({
  open,
  onOpenChange,
  title,
  description,
  reasonLabel = 'Motivo',
  minLength = 10,
  optional = false,
  confirmLabel,
  destructive,
  requireText,
  children,
  onConfirm,
}: ReasonDialogProps) {
  const [reason, setReason] = React.useState('')
  const [typed, setTyped] = React.useState('')
  const [touched, setTouched] = React.useState(false)
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<unknown>(null)

  React.useEffect(() => {
    if (!open) {
      setReason('')
      setTyped('')
      setTouched(false)
      setError(null)
      setPending(false)
    }
  }, [open])

  const len = reason.trim().length
  const tooShort = optional ? len > 0 && len < minLength : len < minLength
  const nameOk = !requireText || typed.trim() === requireText.trim()

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setTouched(true)
    if (tooShort || !nameOk || pending) return
    setPending(true)
    setError(null)
    try {
      await onConfirm(reason.trim())
      onOpenChange(false)
    } catch (err) {
      setError(err)
    } finally {
      setPending(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => (!pending ? onOpenChange(o) : undefined)}>
      <DialogContent size="md">
        <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            {description ? <DialogDescription>{description}</DialogDescription> : null}
          </DialogHeader>
          {children}
          <Field invalid={touched && tooShort} required={!optional}>
            <FieldLabel required={!optional}>{optional ? `${reasonLabel} (opcional)` : reasonLabel}</FieldLabel>
            <Textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={4} onBlur={() => setTouched(true)} />
            <FieldDescription>
              {minLength > 0 ? `Mínimo de ${minLength} caracteres (${len}/${minLength}). ` : ''}Fica registrado na auditoria.
            </FieldDescription>
            <FieldError>{touched && tooShort ? `Escreva ao menos ${minLength} caracteres.` : null}</FieldError>
          </Field>
          {requireText ? (
            <Field>
              <FieldLabel>
                Digite <strong className="font-bold">{requireText}</strong> para confirmar
              </FieldLabel>
              <Input value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" spellCheck={false} />
            </Field>
          ) : null}
          {error ? (
            <Alert variant="danger">
              <AlertDescription>{describeError(error)}</AlertDescription>
            </Alert>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)} disabled={pending}>
              Cancelar
            </Button>
            <Button type="submit" variant={destructive ? 'destructive' : 'primary'} loading={pending} disabled={!nameOk}>
              {confirmLabel}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
