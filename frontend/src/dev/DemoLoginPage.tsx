/**
 * SOMENTE DESENVOLVIMENTO (/_dev/entrar): entra no painel com um perfil fictício para
 * revisão visual (layout, temas, RBAC de menus) enquanto o backend de auth não está pronto.
 * Não chama a API, não cria contas, não envia formulários. Ausente no build de produção.
 */
import { useNavigate } from 'react-router-dom'
import { FlaskConical } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { useAuthStore } from '@/stores/authStore'
import { AuthLayout } from '@/features/auth/components/AuthLayout'
import { DEMO_PROFILES } from './demoProfiles'
import { DEV_DEMO_KEY } from './demoKey'

export default function DemoLoginPage() {
  const enterDemo = useAuthStore((s) => s.enterDemo)
  const navigate = useNavigate()
  return (
    <AuthLayout title="Modo demonstração" description="Escolha um perfil fictício para revisar o painel sem backend." wide>
      <Alert variant="warning">
        <FlaskConical aria-hidden="true" />
        <div>
          <AlertTitle>DEMONSTRAÇÃO — somente desenvolvimento</AlertTitle>
          <AlertDescription className="text-text">
            Sessão fictícia em memória. Nenhum dado exibido é real e nada é enviado ao servidor.
          </AlertDescription>
        </div>
      </Alert>
      <ul className="m-0 flex list-none flex-col gap-3 p-0">
        {DEMO_PROFILES.map((p) => (
          <li key={p.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border p-4">
            <div className="flex min-w-0 flex-col">
              <span className="font-semibold text-text">{p.label}</span>
              <span className="text-xs text-text-muted">{p.description}</span>
            </div>
            <Button
              variant="secondary"
              onClick={() => {
                try {
                  window.sessionStorage.setItem(DEV_DEMO_KEY, p.id)
                } catch {
                  /* ignora */
                }
                enterDemo(p.me)
                navigate('/', { replace: true })
              }}
            >
              Entrar como {p.label}
            </Button>
          </li>
        ))}
      </ul>
    </AuthLayout>
  )
}
