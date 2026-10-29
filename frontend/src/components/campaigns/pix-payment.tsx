/**
 * Pagamento PIX: QR code renderizado LOCALMENTE (lib `qrcode`, sem serviço externo) a partir
 * do código copia-e-cola devolvido pelo servidor (`pix_qr_code`), + botão copiar.
 * Contagem regressiva sempre pelo `expires_at` do servidor.
 */
import * as React from 'react'
import { Copy, Timer, AlertTriangle } from 'lucide-react'
import { toast } from 'sonner'
import { QrCodeSvg } from '@/features/auth/components/QrCodeSvg'
import { Button } from '@/components/ui/button'
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field'
import { Textarea } from '@/components/ui/textarea'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { formatDateTime } from '@/lib/format'
import { formatRemaining, useNow } from '@/pages/campaigns/lib/hooks'

export function Countdown({ expiresAt, onExpire, label = 'Reserva expira em' }: { expiresAt: string | null | undefined; onExpire?: () => void; label?: string }) {
  const target = expiresAt ? new Date(expiresAt).getTime() : NaN
  const valid = Number.isFinite(target)
  const now = useNow(1000, valid)
  const remaining = valid ? target - now : 0
  const fired = React.useRef(false)
  React.useEffect(() => {
    if (valid && remaining <= 0 && !fired.current) {
      fired.current = true
      onExpire?.()
    }
  }, [valid, remaining, onExpire])
  if (!valid) return null
  const expired = remaining <= 0
  return (
    <p className="m-0 flex flex-wrap items-center gap-2 text-sm" data-testid="countdown">
      <Timer aria-hidden="true" className="size-5 text-warning" />
      {expired ? (
        <span className="font-semibold text-danger">Reserva expirada em {formatDateTime(expiresAt)}.</span>
      ) : (
        <>
          <span>{label}</span>
          <span className="font-bold tabular-nums" aria-live="off">
            {formatRemaining(remaining)}
          </span>
          <span className="text-text-muted">(até {formatDateTime(expiresAt)})</span>
        </>
      )}
    </p>
  )
}

export function PixPayment({ code }: { code: string }) {
  const simulated = code.startsWith('FAKE-DEV-PIX-')
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code)
      toast.success('Código PIX copiado.')
    } catch {
      toast.error('Não foi possível copiar. Selecione o código e copie manualmente.')
    }
  }
  return (
    <div className="flex flex-col gap-4">
      {simulated ? (
        <Alert variant="warning">
          <AlertTriangle aria-hidden="true" />
          <AlertDescription>
            Cobrança SIMULADA do ambiente de desenvolvimento (provedor de pagamento falso). Não tente pagar este código.
          </AlertDescription>
        </Alert>
      ) : null}
      <div className="flex flex-col items-start gap-4 md:flex-row">
        <div className="shrink-0 rounded-xl border border-border bg-white p-2">
          <QrCodeSvg value={code} size={208} label="QR code PIX para pagamento" />
        </div>
        <Field className="min-w-0 flex-1">
          <FieldLabel>PIX copia e cola</FieldLabel>
          <Textarea readOnly value={code} rows={5} className="font-mono text-xs" onFocus={(e) => e.currentTarget.select()} />
          <FieldDescription>Abra o app do seu banco, escolha PIX copia e cola e cole o código.</FieldDescription>
          <Button variant="secondary" onClick={() => void copy()} className="w-fit">
            <Copy aria-hidden="true" />
            Copiar código
          </Button>
        </Field>
      </div>
    </div>
  )
}
