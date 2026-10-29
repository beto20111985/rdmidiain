/**
 * Diálogo de ação com MOTIVO obrigatório (auditoria) e, opcionalmente, confirmação forte por
 * texto digitado (nome/código do registro). Erros da ação ficam dentro do diálogo (DS §10).
 * Usado em: inativar estabelecimento, mudar status/mover/retirar tela, comandos remotos,
 * revogar dispositivo, resetar âncora, recusar cadastro, cancelar emergência etc.
 */
import * as React from 'react'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { Input } from '@/components/ui/input'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { errorMessage } from '@/lib/errors'

export interface ReasonDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description?: React.ReactNode
  consequences?: string[]
  confirmLabel: string
  /** Rótulo do botão que fecha sem agir (use "Voltar" quando a ação já é um cancelamento). */
  cancelLabel?: string
  destructive?: boolean
  /** Mínimo de caracteres do motivo (o backend valida; padrão 10). */
  minReason?: number
  reasonLabel?: string
  reasonHint?: string
  /** Texto a digitar para habilitar a confirmação (ex.: código TEL-SP-XXXX). */
  requireText?: string
  /** Campos extras (ex.: novo status, novo estabelecimento) acima do motivo. */
  children?: React.ReactNode
  /** Bloqueia a confirmação enquanto os campos extras forem inválidos. */
  extraValid?: boolean
  onConfirm: (reason: string) => Promise<unknown>
}

export function ReasonDialog({
  open,
  onOpenChange,
  title,
  description,
  consequences,
  confirmLabel,
  cancelLabel = 'Cancelar',
  destructive,
  minReason = 10,
  reasonLabel = 'Motivo',
  reasonHint,
  requireText,
  children,
  extraValid = true,
  onConfirm,
}: ReasonDialogProps) {
  const [reason, setReason] = React.useState('')
  const [typed, setTyped] = React.useState('')
  const [touched, setTouched] = React.useState(false)
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<unknown>(null)
  const reasonRef = React.useRef<HTMLTextAreaElement>(null)
  const typedRef = React.useRef<HTMLInputElement>(null)

  React.useEffect(() => {
    if (!open) {
      setReason('')
      setTyped('')
      setTouched(false)
      setError(null)
      setPending(false)
    }
  }, [open])

  const reasonOk = reason.trim().length >= minReason
  const typedOk = !requireText || typed.trim() === requireText.trim()
  const canConfirm = reasonOk && typedOk && extraValid && !pending

  const submit = async (e?: React.FormEvent) => {
    e?.preventDefault()
    setTouched(true)
    if (!canConfirm) {
      // leva o foco ao primeiro campo inválido (o erro é anunciado pelo aria-describedby do campo)
      if (!reasonOk) reasonRef.current?.focus()
      else if (!typedOk) typedRef.current?.focus()
      return
    }
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
        <form onSubmit={submit} className="flex flex-col gap-5" noValidate>
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
          {children}
          <Field invalid={touched && !reasonOk} required={minReason > 0}>
            <FieldLabel>{reasonLabel}</FieldLabel>
            <Textarea ref={reasonRef} value={reason} onChange={(e) => setReason(e.target.value)} maxLength={2000} rows={3} />
            <FieldDescription>
              {reasonHint ?? `Fica registrado na auditoria. Mínimo de ${minReason} caracteres.`}
            </FieldDescription>
            <FieldError>{touched && !reasonOk ? `Descreva o motivo com pelo menos ${minReason} caracteres.` : null}</FieldError>
          </Field>
          {requireText ? (
            <Field invalid={touched && !typedOk}>
              <FieldLabel>
                Digite <strong className="font-bold">{requireText}</strong> para confirmar
              </FieldLabel>
              <Input
                ref={typedRef}
                value={typed}
                onChange={(e) => setTyped(e.target.value)}
                autoComplete="off"
                autoCapitalize="off"
                spellCheck={false}
              />
              <FieldError>{touched && !typedOk ? 'O texto digitado não confere.' : null}</FieldError>
            </Field>
          ) : null}
          {touched && !extraValid ? (
            <p className="m-0 text-xs font-medium text-danger" role="alert">
              Preencha os campos obrigatórios acima.
            </p>
          ) : null}
          {error ? (
            <Alert variant="danger">
              <AlertDescription className="text-text">{errorMessage(error)}</AlertDescription>
            </Alert>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)} disabled={pending}>
              {cancelLabel}
            </Button>
            <Button type="submit" variant={destructive ? 'destructive' : 'primary'} loading={pending}>
              {confirmLabel}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
