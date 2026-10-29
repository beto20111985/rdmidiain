/**
 * Confirmação de ação (DS §10/§17). Para exclusões, passe `requireText` (ex.: nome da
 * campanha): o botão só habilita quando o texto digitado coincide. Lista as consequências
 * reais. Erros da ação ficam DENTRO do diálogo (não em toast). Foco preso; Esc cancela; ao fechar o
 * foco volta a quem abriu (também quando aberto por estado — padrão de `useReturnFocus`).
 * O texto de apoio da confirmação digitada segue a ação (`destructive`/`confirmHint`).
 *
 * <ConfirmDialog open={o} onOpenChange={setO} destructive title="Excluir a campanha Verão?"
 *   description="Esta ação remove o agendamento." consequences={['As telas deixam de exibir…']}
 *   requireText="Verão" confirmLabel="Excluir campanha" onConfirm={() => mutation.mutateAsync()} />
 */
import * as React from 'react'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { errorMessage } from '@/lib/errors'

export interface ConfirmDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description?: React.ReactNode
  consequences?: string[]
  confirmLabel: string
  cancelLabel?: string
  destructive?: boolean
  /** Texto que o usuário precisa digitar para confirmar (ex.: nome do registro). */
  requireText?: string
  /**
   * Texto de apoio sob o campo de confirmação digitada. Padrão coerente com a ação: só fala em
   * remoção/irreversibilidade quando `destructive`; ações não destrutivas (ex.: publicar) recebem um
   * texto neutro. Passe explicitamente quando houver algo específico (ex.: "Versão publicada não muda").
   */
  confirmHint?: string
  onConfirm: () => Promise<unknown> | unknown
}

export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  consequences,
  confirmLabel,
  cancelLabel = 'Cancelar',
  destructive = false,
  requireText,
  confirmHint,
  onConfirm,
}: ConfirmDialogProps) {
  const [typed, setTyped] = React.useState('')
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<unknown>(null)

  React.useEffect(() => {
    if (!open) {
      setTyped('')
      setError(null)
      setPending(false)
    }
  }, [open])

  const matches = !requireText || typed.trim() === requireText.trim()

  const handleConfirm = async () => {
    if (!matches || pending) return
    setPending(true)
    setError(null)
    try {
      await onConfirm()
      onOpenChange(false)
    } catch (e) {
      setError(e)
    } finally {
      setPending(false)
    }
  }

  return (
    <AlertDialog open={open} onOpenChange={(o) => (!pending ? onOpenChange(o) : undefined)}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          {description ? <AlertDialogDescription>{description}</AlertDialogDescription> : null}
        </AlertDialogHeader>
        {consequences?.length ? (
          <ul className="m-0 flex list-disc flex-col gap-1 pl-5 text-sm text-text">
            {consequences.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
        ) : null}
        {requireText ? (
          <Field>
            <FieldLabel>
              Digite <strong className="font-bold">{requireText}</strong> para confirmar
            </FieldLabel>
            <Input
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              autoComplete="off"
              autoCapitalize="off"
              spellCheck={false}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault()
                  void handleConfirm()
                }
              }}
            />
            <FieldDescription>
              {confirmHint ??
                (destructive
                  ? 'Esta confirmação evita remover ou desfazer algo por engano.'
                  : 'Esta confirmação evita que a ação seja feita por engano.')}
            </FieldDescription>
          </Field>
        ) : null}
        {error ? (
          <Alert variant="danger">
            <AlertDescription className="text-text">{errorMessage(error)}</AlertDescription>
          </Alert>
        ) : null}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={pending}>{cancelLabel}</AlertDialogCancel>
          <Button
            variant={destructive ? 'destructive' : 'primary'}
            disabled={!matches}
            loading={pending}
            onClick={() => void handleConfirm()}
          >
            {confirmLabel}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
