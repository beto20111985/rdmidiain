/**
 * Cadastro do TOTP (aplicativo autenticador) — usado no assistente de primeiro acesso e na
 * ativação voluntária em Minha conta → Segurança.
 *  1) confirmar a senha → POST /auth/mfa/totp/setup → otpauth_uri (QR gerado localmente)
 *  2) digitar o código do app → POST /auth/mfa/totp/enable → códigos de recuperação
 *  3) códigos exibidos UMA única vez; exige a confirmação de que foram guardados.
 * `setupToken` = token de setup do login (só em memória); sem ele usa a sessão atual.
 */
import * as React from 'react'
import { KeyRound } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { isApiError } from '@/lib/errors'
import { authApi } from '../api'
import { setupErrorMessage } from '../authErrors'
import type { TotpEnableResponse, TotpSetup } from '../types'
import { PasswordInput } from './PasswordInput'
import { QrCodeSvg } from './QrCodeSvg'
import { RecoveryCodesPanel } from './RecoveryCodesPanel'

type Step = 'password' | 'scan' | 'codes'

function groupSecret(secret: string) {
  return secret.replace(/\s+/g, '').match(/.{1,4}/g)?.join(' ') ?? secret
}

export interface TotpEnrollmentProps {
  setupToken?: string | null
  passwordLabel?: string
  passwordHint?: string
  /** Mostra "1. Confirmar senha › 2. Ler QR code › 3. Guardar códigos" (some no assistente, que já tem progresso). */
  showSubsteps?: boolean
  /** Chamado quando a pessoa confirma ter guardado os códigos (a sessão devolvida deve ser adotada aqui). */
  onFinished: (res: TotpEnableResponse) => void | Promise<void>
  /** O servidor ativou o TOTP e os códigos de recuperação estão na tela (não redirecionar até confirmar). */
  onEnabled?: () => void
  /** 401 do token de setup (expirou): o chamador volta ao login. */
  onSetupTokenExpired?: () => void
  finishLabel?: string
}

export function TotpEnrollment({
  setupToken,
  passwordLabel = 'Senha atual',
  passwordHint = 'Confirmamos a senha antes de gerar a chave do aplicativo.',
  showSubsteps = true,
  onFinished,
  onEnabled,
  onSetupTokenExpired,
  finishLabel = 'Concluir',
}: TotpEnrollmentProps) {
  const [step, setStep] = React.useState<Step>('password')
  const [password, setPassword] = React.useState('')
  const [setup, setSetup] = React.useState<TotpSetup | null>(null)
  const [code, setCode] = React.useState('')
  const [enableResponse, setEnableResponse] = React.useState<TotpEnableResponse | null>(null)
  const [error, setError] = React.useState<string | null>(null)
  const [pending, setPending] = React.useState(false)

  const tokenExpired = (err: unknown) => isApiError(err) && err.status === 401 && err.code === 'INVALID_TOKEN' && !!setupToken

  const startSetup = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!password) {
      setError('Informe sua senha para continuar.')
      return
    }
    setError(null)
    setPending(true)
    try {
      setSetup(await authApi.totpSetup(password, setupToken))
      setPassword('')
      setStep('scan')
    } catch (err) {
      if (tokenExpired(err)) {
        onSetupTokenExpired?.()
        return
      }
      // 400 INVALID_PASSWORD (backend atual) ou 401 legado sem código próprio = senha incorreta.
      setError(isApiError(err) && err.status === 401 && err.code.startsWith('HTTP_') ? 'Senha incorreta.' : setupErrorMessage(err))
    } finally {
      setPending(false)
    }
  }

  const enable = async (e: React.FormEvent) => {
    e.preventDefault()
    const value = code.replace(/\s+/g, '')
    if (!/^\d{6}$/.test(value)) {
      setError('Digite os 6 números exibidos no aplicativo.')
      return
    }
    setError(null)
    setPending(true)
    try {
      const res = await authApi.totpEnable(value, setupToken)
      setEnableResponse(res)
      setStep('codes')
      onEnabled?.()
    } catch (err) {
      if (tokenExpired(err)) {
        onSetupTokenExpired?.()
        return
      }
      setError(
        isApiError(err) && (err.status === 400 || err.status === 401) && err.code !== 'MFA_SETUP_NOT_STARTED'
          ? 'Código inválido. Confira o horário do celular e tente novamente.'
          : setupErrorMessage(err),
      )
      setCode('')
    } finally {
      setPending(false)
    }
  }

  const finish = async () => {
    if (!enableResponse) return
    setPending(true)
    try {
      await onFinished(enableResponse)
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="flex flex-col gap-6">
      {showSubsteps ? (
        <ol className="m-0 flex list-none flex-wrap gap-2 p-0 text-xs font-semibold text-text-muted" aria-label="Etapas do aplicativo autenticador">
          {(['password', 'scan', 'codes'] as Step[]).map((s, i) => (
            <li key={s} aria-current={step === s ? 'step' : undefined} className={step === s ? 'text-text' : undefined}>
              {i + 1}. {s === 'password' ? 'Confirmar senha' : s === 'scan' ? 'Ler QR code' : 'Guardar códigos'}
              {i < 2 ? <span aria-hidden="true"> ›</span> : null}
            </li>
          ))}
        </ol>
      ) : null}

      {error && step !== 'scan' ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">{error}</AlertDescription>
        </Alert>
      ) : null}

      {step === 'password' ? (
        <form onSubmit={startSetup} noValidate className="flex flex-col gap-6">
          <Field>
            <FieldLabel required>{passwordLabel}</FieldLabel>
            <PasswordInput autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} autoFocus />
            <FieldDescription>{passwordHint}</FieldDescription>
          </Field>
          <Button type="submit" size="block" loading={pending}>
            Continuar
          </Button>
        </form>
      ) : null}

      {step === 'scan' && setup ? (
        <form onSubmit={enable} noValidate className="flex flex-col gap-6">
          <div className="flex flex-col items-center gap-4 sm:flex-row sm:items-start">
            <QrCodeSvg value={setup.otpauth_uri} label="QR code para cadastrar a conta no aplicativo autenticador" />
            <div className="flex flex-col gap-3 text-sm">
              <p className="m-0 text-text">
                1. No celular, abra o <strong>Google Authenticator</strong> (ou outro aplicativo TOTP) e leia o QR code.
              </p>
              <p className="m-0 text-text-muted">Sem câmera? Digite esta chave no aplicativo:</p>
              <code className="break-all rounded-md border border-border bg-field px-3 py-2 font-mono text-sm tracking-wider text-text">
                {groupSecret(setup.secret)}
              </code>
              <p className="m-0 text-text">2. Digite abaixo o código de 6 números que aparece no aplicativo.</p>
            </div>
          </div>
          <Field invalid={!!error}>
            <FieldLabel required>Código do aplicativo</FieldLabel>
            <Input
              value={code}
              onChange={(e) => setCode(e.target.value)}
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={6}
              className="text-center text-[20px] tracking-[0.4em] tabular-nums"
            />
            <FieldError>{error}</FieldError>
          </Field>
          <Button type="submit" size="block" loading={pending} loadingText="Ativando…">
            <KeyRound aria-hidden="true" />
            Ativar verificação
          </Button>
        </form>
      ) : null}

      {step === 'codes' && enableResponse ? (
        <RecoveryCodesPanel codes={enableResponse.recovery_codes ?? []} confirmLabel={finishLabel} pending={pending} onConfirm={finish} />
      ) : null}
    </div>
  )
}
