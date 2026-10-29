/**
 * Assistente de primeiro acesso — `/primeiro-acesso` (SPEC AÇÃO 003 §11.2).
 * Conduz SOMENTE os passos que o servidor diz pendentes, na ordem imposta no backend:
 *   1. Alterar senha · 2. Configurar TOTP · 3. Criar senha financeira (bloqueantes)
 *   4. Convidar o segundo Master (opcional; só na aprovação DUPLA e para o titular de assento).
 * O estado vem do servidor (login `mfa_setup_required`, `GET /auth/setup/*`, `/me.security_setup`);
 * nada é decidido ou guardado aqui. Token de setup e códigos de recuperação ficam só em memória.
 */
import * as React from 'react'
import { Navigate, useNavigate, useSearchParams } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import { ErrorState } from '@/components/patterns/states'
import { BootScreen } from '@/components/auth/boot-screen'
import { Spinner } from '@/components/ui/spinner'
import { isApiError } from '@/lib/errors'
import { tokenStore } from '@/lib/http'
import { useAuthStore } from '@/stores/authStore'
import { useSecurityUiStore } from '@/features/security/uiStore'
import { AuthLayout } from '../components/AuthLayout'
import { StepProgress } from '../components/StepProgress'
import { TotpEnrollment } from '../components/TotpEnrollment'
import { PasswordStep } from '../components/wizard/PasswordStep'
import { FinancialPasswordStep } from '../components/wizard/FinancialPasswordStep'
import { SecondMasterStep } from '../components/wizard/SecondMasterStep'
import { safeNext } from '../authErrors'
import { nextStepOf, resolveSetup, SETUP_STEP_LABEL, wizardSteps } from '../setup'
import type { SetupState, SetupStepCode } from '../types'

const STEP_TITLE: Record<SetupStepCode, string> = {
  PASSWORD_CHANGE: 'Alterar senha',
  TOTP: 'Configurar o autenticador (TOTP)',
  FINANCIAL_PASSWORD: 'Criar a senha financeira',
  SECOND_MASTER: 'Convidar o segundo Administrador Master',
}

const STEP_HELP: Record<SetupStepCode, string> = {
  PASSWORD_CHANGE: 'Sua senha atual é provisória. Crie uma senha nova, só sua, para continuar.',
  TOTP: 'Seu perfil exige verificação em duas etapas. Configure o aplicativo autenticador para acessar o painel.',
  FINANCIAL_PASSWORD: 'Operações financeiras exigem uma senha financeira própria, além do código do autenticador.',
  SECOND_MASTER: 'Esta etapa é opcional agora: você pode convidar depois, mas operações financeiras de dupla aprovação ficam bloqueadas até lá.',
}

export default function FirstAccessWizardPage() {
  const status = useAuthStore((s) => s.status)
  const storeSetup = useAuthStore((s) => s.setup)
  const me = useAuthStore((s) => s.me)
  const setupToken = useAuthStore((s) => s.mfaSetupToken)
  const refreshSetup = useAuthStore((s) => s.refreshSetup)
  const bootstrap = useAuthStore((s) => s.bootstrap)
  const completeMfaSetup = useAuthStore((s) => s.completeMfaSetup)
  const expireSetupToken = useAuthStore((s) => s.expireSetupToken)
  const logout = useAuthStore((s) => s.logout)
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const next = safeNext(params.get('next'))
  const [seen, setSeen] = React.useState<ReadonlySet<SetupStepCode>>(new Set())
  const [loadError, setLoadError] = React.useState<unknown>(null)
  const headingRef = React.useRef<HTMLHeadingElement>(null)
  const lastStep = React.useRef<SetupStepCode | null>(null)

  const setup: SetupState | null = storeSetup ?? (me ? resolveSetup(me) : null)
  const step = setup ? nextStepOf(setup) : null

  // Sem estado conhecido (ex.: recarregou com sessão): consulta o servidor uma vez.
  React.useEffect(() => {
    if (status !== 'setup_required' || storeSetup) return
    refreshSetup().catch((e) => {
      if (isApiError(e) && e.status === 401) expireSetupToken()
      else setLoadError(e)
    })
  }, [status, storeSetup, refreshSetup, expireSetupToken])

  // Registra os passos já vistos como pendentes (o progresso mantém os concluídos nesta visita).
  React.useEffect(() => {
    if (!setup) return
    const add: SetupStepCode[] = []
    if (setup.steps.password_change === 'PENDING') add.push('PASSWORD_CHANGE')
    if (setup.steps.totp === 'PENDING') add.push('TOTP')
    if (setup.steps.financial_password === 'PENDING') add.push('FINANCIAL_PASSWORD')
    if (add.length) setSeen((prev) => (add.every((c) => prev.has(c)) ? prev : new Set([...prev, ...add])))
  }, [setup])

  // Ao mudar de etapa, o foco vai para o título dela (leitores de tela anunciam a nova etapa).
  React.useEffect(() => {
    if (step && lastStep.current && lastStep.current !== step) headingRef.current?.focus()
    lastStep.current = step
  }, [step])

  if (status === 'booting') return <BootScreen />
  if (status === 'anonymous') return <Navigate to="/login" replace />
  if (status === 'mfa_required') return <Navigate to="/login/verificacao" replace />
  if (status === 'unavailable' || loadError) {
    return (
      <AuthLayout title="Primeiro acesso">
        <ErrorState
          error={loadError}
          title="Não foi possível verificar sua sessão"
          description="O servidor não respondeu. Verifique sua conexão e tente novamente."
          onRetry={() => {
            setLoadError(null)
            void bootstrap()
          }}
        />
      </AuthLayout>
    )
  }
  if (!setup) {
    return (
      <AuthLayout title="Primeiro acesso" description="Carregando o estado da sua configuração…">
        <div className="flex justify-center py-6">
          <Spinner label="Carregando…" />
        </div>
      </AuthLayout>
    )
  }
  if (!step) return <Navigate to={next} replace />
  // 2º Master: SOMENTE na DUPLA e para o titular de assento. Em qualquer outro caso, nada a mostrar.
  if (step === 'SECOND_MASTER' && !(setup.is_admin_master && setup.approval_mode === 'DUAL')) return <Navigate to={next} replace />

  const steps = wizardSteps(setup, seen)
  const position = Math.max(0, steps.findIndex((s) => s.code === step))
  const hasSession = !!tokenStore.get()
  const blocking = status === 'setup_required'

  const finishSecondMaster = () => {
    useSecurityUiStore.getState().dismissSecondMasterModal()
    navigate(next, { replace: true })
  }

  return (
    <AuthLayout
      wide
      title="Primeiro acesso"
      description={step === 'SECOND_MASTER' ? 'Última etapa da configuração de segurança.' : 'Conclua as etapas de segurança para acessar o painel.'}
      footer={
        blocking ? (
          <Button variant="tertiary" onClick={() => void logout()}>
            Sair e continuar depois
          </Button>
        ) : null
      }
    >
      <StepProgress steps={steps.map((s) => ({ key: s.code, label: SETUP_STEP_LABEL[s.code], state: s.state }))} />
      <section aria-labelledby="wizard-step-title" className="flex flex-col gap-6">
        <div className="flex flex-col gap-1">
          <h2 id="wizard-step-title" ref={headingRef} tabIndex={-1} className="m-0 text-title-section text-text outline-none">
            {steps.length > 1 ? `${position + 1}. ` : ''}
            {STEP_TITLE[step]}
          </h2>
          <p className="m-0 text-sm text-text-muted">{STEP_HELP[step]}</p>
        </div>

        {step === 'PASSWORD_CHANGE' ? <PasswordStep /> : null}
        {step === 'TOTP' ? (
          <TotpEnrollment
            key="totp"
            setupToken={hasSession ? null : setupToken}
            showSubsteps={false}
            passwordLabel="Confirme sua senha"
            passwordHint="Digite a senha que você acabou de criar para gerar a chave do aplicativo."
            finishLabel="Continuar"
            onSetupTokenExpired={expireSetupToken}
            onFinished={(res) => completeMfaSetup(res)}
          />
        ) : null}
        {step === 'FINANCIAL_PASSWORD' ? <FinancialPasswordStep /> : null}
        {step === 'SECOND_MASTER' ? <SecondMasterStep status={setup.steps.second_master} onFinish={finishSecondMaster} /> : null}
      </section>
    </AuthLayout>
  )
}
