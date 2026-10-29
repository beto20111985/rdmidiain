/**
 * Estados globais do DS §10: vazio com explicação e ação útil, erro com tentar novamente,
 * sem permissão com orientação, dados desatualizados com horário, carregamento em esqueleto.
 */
import * as React from 'react'
import { AlertTriangle, Clock, Inbox, Lock, RefreshCw, WifiOff, type LucideIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty'
import { cn } from '@/lib/utils'
import { errorMessage, isApiError } from '@/lib/errors'
import { formatDateTime, formatTime } from '@/lib/format'

type HeadingLevel = 'h2' | 'h3'

// ---------------------------------------------------------------------------
export interface EmptyStateProps {
  title: string
  description?: React.ReactNode
  icon?: LucideIcon
  action?: React.ReactNode
  className?: string
  headingLevel?: HeadingLevel
  /** compact = dentro de cartões/tabelas */
  size?: 'page' | 'compact'
}

export function EmptyState({ title, description, icon: Icon = Inbox, action, className, headingLevel = 'h2', size = 'page' }: EmptyStateProps) {
  const Title = headingLevel
  return (
    <Empty className={cn(size === 'compact' && 'p-4 md:p-6', className)} data-state="empty">
      <EmptyHeader>
        <EmptyMedia>
          <Icon />
        </EmptyMedia>
        <Title className={cn('text-text', size === 'compact' ? 'text-title-card' : 'text-title-section')}>{title}</Title>
        {description ? <EmptyDescription>{description}</EmptyDescription> : null}
      </EmptyHeader>
      {action ? <EmptyContent>{action}</EmptyContent> : null}
    </Empty>
  )
}

// ---------------------------------------------------------------------------
export interface ErrorStateProps {
  error?: unknown
  title?: string
  description?: React.ReactNode
  onRetry?: () => void
  retrying?: boolean
  className?: string
  size?: 'page' | 'compact'
}

/** Erro com "Tentar novamente". Mostra a mensagem do servidor e o ID da requisição (suporte). */
export function ErrorState({ error, title, description, onRetry, retrying, className, size = 'page' }: ErrorStateProps) {
  const network = isApiError(error) && error.isNetwork
  const Icon = network ? WifiOff : AlertTriangle
  const requestId = isApiError(error) ? error.requestId : undefined
  return (
    <Empty role="alert" data-state="error" className={cn(size === 'compact' && 'p-4 md:p-6', className)}>
      <EmptyHeader>
        <EmptyMedia className="text-danger">
          <Icon />
        </EmptyMedia>
        <EmptyTitle className={size === 'compact' ? 'text-title-card' : undefined}>
          {title ?? (network ? 'Sem conexão com o servidor' : 'Não foi possível carregar')}
        </EmptyTitle>
        <EmptyDescription>{description ?? errorMessage(error)}</EmptyDescription>
        {requestId ? <p className="text-xs text-text-muted">Código para o suporte: {requestId}</p> : null}
      </EmptyHeader>
      {onRetry ? (
        <EmptyContent>
          <Button variant="secondary" onClick={onRetry} loading={retrying} loadingText="Tentando novamente…">
            <RefreshCw aria-hidden="true" />
            Tentar novamente
          </Button>
        </EmptyContent>
      ) : null}
    </Empty>
  )
}

// ---------------------------------------------------------------------------
export interface NoPermissionProps {
  /** Permissão que faltou (código do catálogo) — exibida para orientar o pedido de acesso. */
  permission?: string | null
  title?: string
  description?: React.ReactNode
  action?: React.ReactNode
  className?: string
}

/** Sem permissão, com orientação (DS §10). Use também para 403 vindo da API. */
export function NoPermission({ permission, title = 'Você não tem acesso a esta área', description, action, className }: NoPermissionProps) {
  return (
    <Empty data-state="no-permission" className={className}>
      <EmptyHeader>
        <EmptyMedia className="text-warning">
          <Lock />
        </EmptyMedia>
        <EmptyTitle>{title}</EmptyTitle>
        <EmptyDescription>
          {description ??
            'Seu perfil não inclui esta permissão. Se você precisa deste acesso, peça a um administrador da sua conta para liberá-lo.'}
        </EmptyDescription>
        {permission ? (
          <p className="text-xs text-text-muted">
            Permissão necessária: <code className="rounded bg-surface-hover px-1.5 py-0.5 text-text">{permission}</code>
          </p>
        ) : null}
      </EmptyHeader>
      {action ? <EmptyContent>{action}</EmptyContent> : null}
    </Empty>
  )
}

// ---------------------------------------------------------------------------
export interface StaleDataProps {
  /** Momento dos dados exibidos (ms epoch, ISO ou Date). */
  updatedAt: number | string | Date | null | undefined
  /** Motivo: offline / tempo real indisponível / erro ao atualizar. */
  reason?: string
  onRefresh?: () => void
  refreshing?: boolean
  className?: string
}

/** Aviso de dados possivelmente desatualizados, sempre com horário (DS §10). */
export function StaleData({ updatedAt, reason, onRefresh, refreshing, className }: StaleDataProps) {
  const ts = updatedAt ? new Date(updatedAt) : null
  return (
    <div
      role="status"
      data-state="stale"
      className={cn('flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-warning/50 bg-warning-soft px-4 py-2 text-sm text-text', className)}
    >
      <Clock aria-hidden="true" className="size-5 shrink-0 text-warning" />
      <span>
        {ts ? (
          <>
            Dados de <time dateTime={ts.toISOString()}>{formatDateTime(ts)}</time>
            {reason ? ` — ${reason}` : ' — podem estar desatualizados.'}
          </>
        ) : (
          (reason ?? 'Dados possivelmente desatualizados.')
        )}
      </span>
      {onRefresh ? (
        <Button variant="tertiary" onClick={onRefresh} loading={refreshing} className="ml-auto">
          <RefreshCw aria-hidden="true" />
          Atualizar agora
        </Button>
      ) : null}
    </div>
  )
}

/** "Atualizado às HH:MM" para cabeçalhos de cartões e páginas. */
export function UpdatedAt({ at, className }: { at: number | string | Date | null | undefined; className?: string }) {
  if (!at) return null
  const d = new Date(at)
  return (
    <p className={cn('text-xs text-text-muted', className)}>
      Atualizado às <time dateTime={d.toISOString()}>{formatTime(d)}</time>
    </p>
  )
}

// ---------------------------------------------------------------------------
/** Esqueleto genérico de página (carga inicial). */
export function PageSkeleton({ label = 'Carregando conteúdo…' }: { label?: string }) {
  return (
    <div role="status" aria-live="polite" className="flex flex-col gap-6">
      <span className="sr-only">{label}</span>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4 xl:gap-6">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-[132px] rounded-xl" />
        ))}
      </div>
      <Skeleton className="h-[320px] rounded-xl" />
    </div>
  )
}

/** Rótulo obrigatório para qualquer dado fictício (DS §18, E2 #1342–1345). */
export function DemoBadge({ className, children = 'DEMONSTRAÇÃO' }: { className?: string; children?: React.ReactNode }) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-md border border-warning/60 bg-warning-soft px-2 py-1 text-badge uppercase tracking-metric text-warning',
        className,
      )}
    >
      <AlertTriangle aria-hidden="true" className="size-3.5" />
      {children}
    </span>
  )
}
