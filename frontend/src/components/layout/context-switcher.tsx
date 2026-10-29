/**
 * Seletor de contexto (E2 #779–784): a mesma conta alterna entre "RD Mídia Indoor",
 * "Minha unidade — X" e "Minhas campanhas — X" conforme GET /me.contexts. A troca envia
 * `X-RD-Context`, limpa o cache e leva à página inicial do contexto.
 * Com um único contexto, mostra apenas o rótulo (sem menu).
 */
import { useNavigate } from 'react-router-dom'
import { Building2, Check, ChevronsUpDown, Megaphone, Store } from 'lucide-react'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { CONTEXT_KIND_TO_ROUTE, useActiveContext, useContextStore } from '@/stores/contextStore'
import { firstAccessiblePath } from '@/app/routes'
import { usePermissions } from '@/hooks/usePermissions'
import type { ContextKind } from '@/features/auth/types'
import { cn } from '@/lib/utils'

const ICON: Record<ContextKind, typeof Building2> = {
  PLATFORM: Building2,
  ESTABLISHMENT: Store,
  ADVERTISER: Megaphone,
}

export function ContextSwitcher({ className }: { className?: string }) {
  const contexts = useContextStore((s) => s.contexts)
  const setActive = useContextStore((s) => s.setActive)
  const active = useActiveContext()
  const permissions = usePermissions()
  const navigate = useNavigate()
  if (!active) return null
  const ActiveIcon = ICON[active.kind]

  if (contexts.length < 2) {
    return (
      <p className={cn('m-0 flex min-w-0 items-center gap-1.5 text-xs text-text-muted', className)}>
        <ActiveIcon aria-hidden="true" className="size-4 shrink-0" />
        <span className="truncate">{active.label}</span>
      </p>
    )
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        className={cn(
          '-ml-2 inline-flex min-h-11 max-w-full items-center gap-1.5 rounded-md px-2 text-xs font-medium text-text-muted transition-colors duration-fast ease-rd hover:bg-surface-hover hover:text-text',
          className,
        )}
        aria-label={`Contexto atual: ${active.label}. Trocar contexto`}
      >
        <ActiveIcon aria-hidden="true" className="size-4 shrink-0" />
        <span className="truncate">{active.label}</span>
        <ChevronsUpDown aria-hidden="true" className="size-4 shrink-0" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-72">
        <DropdownMenuLabel>Trabalhar como</DropdownMenuLabel>
        <DropdownMenuGroup>
          {contexts.map((c) => {
            const Icon = ICON[c.kind]
            const isActive = c.key === active.key
            return (
              <DropdownMenuItem
                key={c.key}
                onSelect={() => {
                  if (isActive) return
                  setActive(c.key)
                  navigate(firstAccessiblePath(CONTEXT_KIND_TO_ROUTE[c.kind], permissions))
                }}
                aria-current={isActive ? 'true' : undefined}
              >
                <Icon aria-hidden="true" />
                <span className="flex-1">{c.label}</span>
                {isActive ? <Check aria-hidden="true" className="text-link" /> : null}
              </DropdownMenuItem>
            )
          })}
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
