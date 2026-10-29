/**
 * Roteador gerado a partir de routes.ts (fonte única).
 * - Rotas públicas de autenticação.
 * - Rotas do painel dentro de <RequireAuth> + <AppShell>, cada uma com guarda de contexto e
 *   permissão; páginas sem `load` mostram <NotImplementedYet>.
 * O web player NÃO faz parte deste app (vive em player-web/).
 */
import * as React from 'react'
import { Link, Navigate, Route, Routes } from 'react-router-dom'
import { ROUTES, firstAccessiblePath, canAccessRoute, type AppRoute } from './routes'
import { AppShell } from '@/components/layout/app-shell'
import { RequireAuth } from '@/components/auth/guards'
import { NotImplementedYet } from '@/components/patterns/not-implemented-yet'
import { NoPermission, PageSkeleton } from '@/components/patterns/states'
import { Button } from '@/components/ui/button'
import { usePermissions } from '@/hooks/usePermissions'
import { CONTEXT_KIND_TO_ROUTE, useActiveRouteContext, useContextStore } from '@/stores/contextStore'
import { useAuthStore } from '@/stores/authStore'
import { BootScreen } from '@/components/auth/boot-screen'

const LoginPage = React.lazy(() => import('@/features/auth/pages/LoginPage'))
const MfaVerifyPage = React.lazy(() => import('@/features/auth/pages/MfaVerifyPage'))
const MfaSetupPage = React.lazy(() => import('@/features/auth/pages/MfaSetupPage'))
const ForgotPasswordPage = React.lazy(() => import('@/features/auth/pages/ForgotPasswordPage'))
const ResetPasswordPage = React.lazy(() => import('@/features/auth/pages/ResetPasswordPage'))
const AcceptInvitePage = React.lazy(() => import('@/features/auth/pages/AcceptInvitePage'))
// AÇÃO 003: assistente de primeiro acesso e ativação pública do 2º Master (token no fragmento da URL)
const FirstAccessWizardPage = React.lazy(() => import('@/features/auth/pages/FirstAccessWizardPage'))
const MasterActivationPage = React.lazy(() => import('@/features/auth/pages/MasterActivationPage'))
const NotFoundPage = React.lazy(() => import('@/features/system/pages/NotFoundPage'))
// FE-OPS: auto-cadastro público do estabelecimento parceiro (token do e-mail só em memória/header)
const PublicPartnerSignupPage = React.lazy(() => import('@/pages/ops/onboarding/PublicPartnerSignupPage'))
// FE-OPS: auto-cadastro público do anunciante (token do e-mail só em memória/header X-RD-Signup-Token)
const AdvertiserSignupPage = React.lazy(() => import('@/pages/ops/advertiser-signup/AdvertiserSignupPage'))
const DevDemoLoginPage = import.meta.env.DEV ? React.lazy(() => import('@/dev/DemoLoginPage')) : null

const lazyCache = new Map<string, React.LazyExoticComponent<React.ComponentType>>()
function lazyFor(route: AppRoute) {
  if (!route.load) return null
  let c = lazyCache.get(route.id)
  if (!c) {
    c = React.lazy(route.load)
    lazyCache.set(route.id, c)
  }
  return c
}

/** Guarda por rota: contexto (troca automática se o usuário tiver um contexto compatível) e permissão. */
export function RouteGuard({ route }: { route: AppRoute }) {
  const permissions = usePermissions()
  const activeCtx = useActiveRouteContext()
  const contexts = useContextStore((s) => s.contexts)
  const setActive = useContextStore((s) => s.setActive)

  const mustChangePassword = useAuthStore((s) => Boolean(s.me?.must_change_password ?? s.me?.user.must_change_password))
  const compatible = activeCtx !== null && route.contexts.includes(activeCtx)
  const alternative = compatible ? null : contexts.find((c) => route.contexts.includes(CONTEXT_KIND_TO_ROUTE[c.kind]))

  React.useEffect(() => {
    if (!compatible && alternative) setActive(alternative.key)
  }, [compatible, alternative, setActive])

  // Senha provisória: só a tela de Segurança fica acessível até a troca.
  if (mustChangePassword && route.id !== 'common.security') return <Navigate to="/minha-conta/seguranca" replace />

  if (!compatible) {
    if (alternative) return <PageSkeleton label="Trocando de contexto…" />
    return (
      <NoPermission
        title="Esta página não está disponível no seu acesso"
        description="Nenhum dos seus contextos (RD, Minha unidade ou Minhas campanhas) dá acesso a esta área."
        action={
          <Button asChild variant="secondary">
            <Link to="/">Ir para o início</Link>
          </Button>
        }
      />
    )
  }

  if (!canAccessRoute(route, permissions)) {
    return (
      <NoPermission
        permission={route.permission}
        action={
          <Button asChild variant="secondary">
            <Link to="/">Ir para o início</Link>
          </Button>
        }
      />
    )
  }

  const Page = lazyFor(route)
  return Page ? <Page /> : <NotImplementedYet route={route} />
}

function HomeRedirect() {
  const permissions = usePermissions()
  const ctx = useActiveRouteContext()
  if (!ctx) return <BootScreen />
  return <Navigate to={firstAccessiblePath(ctx, permissions)} replace />
}

export function AppRouter() {
  return (
    <React.Suspense fallback={<BootScreen />}>
      <Routes>
        {/* Autenticação (públicas) */}
        <Route path="/login" element={<LoginPage />} />
        <Route path="/login/verificacao" element={<MfaVerifyPage />} />
        <Route path="/login/configurar-2fa" element={<MfaSetupPage />} />
        {/* Primeiro acesso: troca de senha → TOTP → senha financeira (→ convite do 2º Master, só na dupla) */}
        <Route path="/primeiro-acesso" element={<FirstAccessWizardPage />} />
        {/* Pública: o token do convite vem no fragmento (#token=…) e é removido da barra na hora */}
        <Route path="/ativar-master" element={<MasterActivationPage />} />
        <Route path="/esqueci-senha" element={<ForgotPasswordPage />} />
        <Route path="/redefinir-senha" element={<ResetPasswordPage />} />
        <Route path="/convite/:token" element={<AcceptInvitePage />} />
        {/* Formato do link enviado pelo backend: /convite?token=... */}
        <Route path="/convite" element={<AcceptInvitePage />} />
        <Route path="/cadastro-parceiro" element={<PublicPartnerSignupPage />} />
        <Route path="/cadastro-anunciante" element={<AdvertiserSignupPage />} />
        {DevDemoLoginPage ? <Route path="/_dev/entrar" element={<DevDemoLoginPage />} /> : null}

        {/* Painel autenticado */}
        <Route
          element={
            <RequireAuth>
              <AppShell />
            </RequireAuth>
          }
        >
          <Route index element={<HomeRedirect />} />
          {ROUTES.map((r) => (
            <Route key={r.id} path={r.path} element={<RouteGuard route={r} />} />
          ))}
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Routes>
    </React.Suspense>
  )
}
