/**
 * Menu da conta: identidade, preferência de tema (escuro/claro/sistema — persistente e
 * sincronizada com o perfil), Minha conta, Segurança, Sessões e Sair (revoga a sessão no servidor).
 */
import { Link, useNavigate } from 'react-router-dom'
import { FileLock2, LogOut, Monitor, Moon, ShieldCheck, Smartphone, Sun, UserRound } from 'lucide-react'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useAuthStore } from '@/stores/authStore'
import { isThemePreference, useThemeStore } from '@/stores/themeStore'
import { getInitials } from '@/lib/utils'

export function UserMenu() {
  const me = useAuthStore((s) => s.me)
  const logout = useAuthStore((s) => s.logout)
  const preference = useThemeStore((s) => s.preference)
  const setPreference = useThemeStore((s) => s.setPreference)
  const navigate = useNavigate()
  const name = me?.user.name ?? 'Usuário'

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        aria-label={`Conta de ${name}`}
        className="inline-flex size-11 items-center justify-center rounded-full transition-colors duration-fast ease-rd hover:bg-surface-hover"
      >
        <span
          aria-hidden="true"
          className="inline-flex size-9 items-center justify-center rounded-full bg-[rgb(var(--nav-active-to))] text-xs font-bold text-[rgb(var(--nav-active-text))]"
        >
          {getInitials(name)}
        </span>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        <DropdownMenuLabel className="flex flex-col gap-0.5">
          <span className="truncate text-sm font-semibold text-text">{name}</span>
          <span className="truncate font-normal">{me?.user.email}</span>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuGroup>
          <DropdownMenuItem asChild>
            <Link to="/minha-conta">
              <UserRound aria-hidden="true" />
              Minha conta
            </Link>
          </DropdownMenuItem>
          <DropdownMenuItem asChild>
            <Link to="/minha-conta/seguranca">
              <ShieldCheck aria-hidden="true" />
              Segurança
            </Link>
          </DropdownMenuItem>
          <DropdownMenuItem asChild>
            <Link to="/minha-conta/sessoes">
              <Smartphone aria-hidden="true" />
              Sessões ativas
            </Link>
          </DropdownMenuItem>
          <DropdownMenuItem asChild>
            <Link to="/minha-conta/privacidade">
              <FileLock2 aria-hidden="true" />
              Privacidade e dados
            </Link>
          </DropdownMenuItem>
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuLabel>Tema</DropdownMenuLabel>
        <DropdownMenuRadioGroup value={preference} onValueChange={(v) => isThemePreference(v) && setPreference(v)}>
          <DropdownMenuRadioItem value="dark">
            <Moon aria-hidden="true" />
            Escuro
          </DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="light">
            <Sun aria-hidden="true" />
            Claro
          </DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="system">
            <Monitor aria-hidden="true" />
            Igual ao sistema
          </DropdownMenuRadioItem>
        </DropdownMenuRadioGroup>
        <DropdownMenuSeparator />
        <DropdownMenuGroup>
          <DropdownMenuItem
            onSelect={async () => {
              await logout()
              navigate('/login', { replace: true })
            }}
          >
            <LogOut aria-hidden="true" />
            Sair
          </DropdownMenuItem>
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
