/**
 * Peças pequenas reutilizadas pelas páginas de Administração, Suporte, Design e
 * Notificações: badge por tom, lista de definições, aviso de dados desatualizados quando
 * o tempo real cai, diálogo de motivo obrigatório e diff antes/depois (sem HTML do servidor).
 */
import * as React from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { Input } from '@/components/ui/input'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { StaleData } from '@/components/patterns/states'
import { useRealtimeStatus } from '@/lib/realtime/context'
import { errorMessage } from '@/lib/errors'
import { EMPTY_VALUE } from '@/lib/format'
import { cn } from '@/lib/utils'
import type { LabelTone } from './labels'

export function ToneBadge({ value, className, icon }: { value: LabelTone; className?: string; icon?: React.ReactNode }) {
  return (
    <Badge tone={value.tone} className={className}>
      {icon}
      {value.label}
    </Badge>
  )
}

export interface KVItem {
  label: string
  value: React.ReactNode
  /** Ocupa a linha inteira. */
  wide?: boolean
}

/** Lista de definições responsiva (2 colunas ≥ 768 px). */
/**
 * A barra do topo esconde a descrição da página abaixo de `md`. Quando a descrição traz
 * informação essencial (situação, prioridade), repita-a no início do conteúdo só no celular.
 */
export function MobileMeta({ children }: { children: React.ReactNode }) {
  return <div className="flex flex-wrap items-center gap-2 text-xs text-text-muted md:hidden">{children}</div>
}

export function KeyValueList({ items, className }: { items: KVItem[]; className?: string }) {
  return (
    <dl className={cn('m-0 grid grid-cols-1 gap-x-6 gap-y-4 md:grid-cols-2', className)}>
      {items.map((it) => (
        <div key={it.label} className={cn('flex min-w-0 flex-col gap-0.5', it.wide && 'md:col-span-2')}>
          <dt className="text-xs text-text-muted">{it.label}</dt>
          <dd className="m-0 min-w-0 break-words text-sm text-text">
            {it.value === null || it.value === undefined || it.value === '' ? EMPTY_VALUE : it.value}
          </dd>
        </div>
      ))}
    </dl>
  )
}

/**
 * Dados possivelmente desatualizados: aparece quando o tempo real (WS) não está ativo
 * e já há dados exibidos. Nunca usa polling — oferece "Atualizar agora".
 */
export function RealtimeStale({
  updatedAt,
  onRefresh,
  refreshing,
  className,
}: {
  updatedAt: number | undefined
  onRefresh?: () => void
  refreshing?: boolean
  className?: string
}) {
  const { status } = useRealtimeStatus()
  if (!updatedAt) return null
  if (status !== 'reconnecting' && status !== 'unavailable' && status !== 'closed') return null
  return (
    <StaleData
      updatedAt={updatedAt}
      reason={status === 'reconnecting' ? 'reconectando ao tempo real; os dados podem estar desatualizados.' : 'tempo real indisponível; os dados podem estar desatualizados.'}
      onRefresh={onRefresh}
      refreshing={refreshing}
      className={className}
    />
  )
}

export interface ReasonDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description?: React.ReactNode
  /** Lista de consequências reais da ação. */
  consequences?: string[]
  reasonLabel?: string
  reasonHint?: string
  /** Mínimo de caracteres (o backend valida também). 0 = opcional. */
  minLength?: number
  maxLength?: number
  /** Texto que precisa ser digitado para confirmar (ações destrutivas). */
  requireText?: string
  confirmLabel: string
  destructive?: boolean
  onConfirm: (reason: string) => Promise<unknown> | unknown
  children?: React.ReactNode
}

/** Diálogo de confirmação com motivo (impersonation, inativar, revogar sessões…). Erro fica no diálogo. */
export function ReasonDialog({
  open,
  onOpenChange,
  title,
  description,
  consequences,
  reasonLabel = 'Motivo',
  reasonHint,
  minLength = 10,
  maxLength = 1000,
  requireText,
  confirmLabel,
  destructive,
  onConfirm,
  children,
}: ReasonDialogProps) {
  const [reason, setReason] = React.useState('')
  const [typed, setTyped] = React.useState('')
  const [pending, setPending] = React.useState(false)
  const [error, setError] = React.useState<unknown>(null)
  const [touched, setTouched] = React.useState(false)

  React.useEffect(() => {
    if (!open) {
      setReason('')
      setTyped('')
      setError(null)
      setPending(false)
      setTouched(false)
    }
  }, [open])

  const trimmed = reason.trim()
  const reasonOk = trimmed.length >= minLength
  const typedOk = !requireText || typed.trim() === requireText.trim()
  const reasonError =
    touched && !reasonOk
      ? minLength > 0
        ? `Informe ${reasonLabel.toLowerCase()} com pelo menos ${minLength} caracteres (${trimmed.length}/${minLength}).`
        : undefined
      : undefined

  const submit = async () => {
    setTouched(true)
    if (!reasonOk || !typedOk || pending) return
    setPending(true)
    setError(null)
    try {
      await onConfirm(trimmed)
      onOpenChange(false)
    } catch (e) {
      setError(e)
    } finally {
      setPending(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => (!pending ? onOpenChange(o) : undefined)}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {description ? <DialogDescription>{description}</DialogDescription> : null}
        </DialogHeader>
        <form
          noValidate
          className="flex flex-col gap-5"
          onSubmit={(e) => {
            e.preventDefault()
            void submit()
          }}
        >
          {consequences?.length ? (
            <ul className="m-0 flex list-disc flex-col gap-1 pl-5 text-sm text-text">
              {consequences.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
          ) : null}
          {children}
          <FieldGroup>
            <Field invalid={!!reasonError} required={minLength > 0}>
              <FieldLabel>{reasonLabel}</FieldLabel>
              <Textarea
                value={reason}
                maxLength={maxLength}
                onChange={(e) => setReason(e.target.value)}
                onBlur={() => setTouched(true)}
              />
              {reasonHint ? <FieldDescription>{reasonHint}</FieldDescription> : null}
              <FieldError>{reasonError}</FieldError>
            </Field>
            {requireText ? (
              <Field>
                <FieldLabel>
                  Digite <strong className="font-bold">{requireText}</strong> para confirmar
                </FieldLabel>
                <Input value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" spellCheck={false} />
              </Field>
            ) : null}
          </FieldGroup>
          {error ? (
            <Alert variant="danger">
              <AlertDescription className="text-text">{errorMessage(error)}</AlertDescription>
            </Alert>
          ) : null}
          <DialogFooter>
            <Button variant="secondary" onClick={() => onOpenChange(false)} disabled={pending}>
              Cancelar
            </Button>
            <Button type="submit" variant={destructive ? 'destructive' : 'primary'} loading={pending} disabled={!reasonOk || !typedOk}>
              {confirmLabel}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function stringify(v: unknown): string {
  if (v === null || v === undefined) return EMPTY_VALUE
  if (typeof v === 'string') return v
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  try {
    return JSON.stringify(v)
  } catch {
    return String(v)
  }
}

/** Antes × depois por campo (dados já mascarados pelo servidor). Renderiza texto puro. */
export function BeforeAfter({ before, after }: { before: unknown; after: unknown }) {
  const b = before && typeof before === 'object' && !Array.isArray(before) ? (before as Record<string, unknown>) : null
  const a = after && typeof after === 'object' && !Array.isArray(after) ? (after as Record<string, unknown>) : null
  if (!b && !a) {
    if (before === null && after === null) return <p className="text-sm text-text-muted">Sem dados de antes/depois neste registro.</p>
    return (
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <pre className="m-0 overflow-x-auto whitespace-pre-wrap break-all rounded-md bg-field p-3 text-xs text-text">{stringify(before)}</pre>
        <pre className="m-0 overflow-x-auto whitespace-pre-wrap break-all rounded-md bg-field p-3 text-xs text-text">{stringify(after)}</pre>
      </div>
    )
  }
  const keys = Array.from(new Set([...Object.keys(b ?? {}), ...Object.keys(a ?? {})])).sort()
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-left text-sm">
        <caption className="sr-only">Valores antes e depois da alteração</caption>
        <thead>
          <tr className="border-b border-border text-xs text-text-muted">
            <th scope="col" className="py-2 pr-3 font-semibold">Campo</th>
            <th scope="col" className="py-2 pr-3 font-semibold">Antes</th>
            <th scope="col" className="py-2 font-semibold">Depois</th>
          </tr>
        </thead>
        <tbody>
          {keys.map((k) => {
            const bv = stringify(b?.[k])
            const av = stringify(a?.[k])
            const changed = bv !== av
            return (
              <tr key={k} className="border-b border-border align-top last:border-0">
                <th scope="row" className="py-2 pr-3 font-mono text-xs font-normal text-text-muted">{k}</th>
                <td className="max-w-[280px] break-all py-2 pr-3 font-mono text-xs text-text">{b ? bv : EMPTY_VALUE}</td>
                <td className={cn('max-w-[280px] break-all py-2 font-mono text-xs', changed ? 'font-semibold text-text' : 'text-text-muted')}>
                  {a ? av : EMPTY_VALUE}
                  {changed && b && a ? <span className="sr-only"> (alterado)</span> : null}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/** Título de seção (h2) padronizado do DS. */
export function SectionTitle({ children, as: Tag = 'h2', className }: { children: React.ReactNode; as?: 'h2' | 'h3'; className?: string }) {
  return <Tag className={cn(Tag === 'h2' ? 'text-title-section' : 'text-title-card', 'text-text', className)}>{children}</Tag>
}
