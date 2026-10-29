/**
 * Ativação VOLUNTÁRIA do 2FA (TOTP) a partir de Minha conta → Segurança.
 * O 2FA OBRIGATÓRIO faz parte do assistente de primeiro acesso (`/primeiro-acesso`); quem chega
 * aqui com o primeiro acesso pendente (ex.: link antigo `/login/configurar-2fa`) é redirecionado.
 */
import * as React from 'react'
import { Link, Navigate, useNavigate } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import { BootScreen } from '@/components/auth/boot-screen'
import { useAuthStore } from '@/stores/authStore'
import { AuthLayout } from '../components/AuthLayout'
import { TotpEnrollment } from '../components/TotpEnrollment'

export default function MfaSetupPage() {
  const status = useAuthStore((s) => s.status)
  const me = useAuthStore((s) => s.me)
  const reloadMe = useAuthStore((s) => s.reloadMe)
  const navigate = useNavigate()
  // Com os códigos de recuperação na tela, nenhuma atualização de /me pode redirecionar (eles só aparecem uma vez).
  const [showingCodes, setShowingCodes] = React.useState(false)

  if (status === 'booting') return <BootScreen />
  if (status === 'setup_required') return <Navigate to="/primeiro-acesso" replace />
  if (status === 'anonymous' || status === 'mfa_required') return <Navigate to="/login" replace />
  if (status === 'authenticated' && me?.user.mfa_enabled && !showingCodes) return <Navigate to="/minha-conta/seguranca" replace />

  return (
    <AuthLayout
      wide
      title="Ativar verificação em duas etapas"
      description="Proteja sua conta com um código do aplicativo autenticador a cada login."
      footer={
        <Button variant="tertiary" asChild>
          <Link to="/minha-conta/seguranca">Cancelar e voltar</Link>
        </Button>
      }
    >
      <TotpEnrollment
        onEnabled={() => setShowingCodes(true)}
        onFinished={async () => {
          await reloadMe()
          navigate('/minha-conta/seguranca', { replace: true })
        }}
      />
    </AuthLayout>
  )
}
