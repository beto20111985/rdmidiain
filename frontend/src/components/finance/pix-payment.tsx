/** Bloco de pagamento PIX: QR gerado LOCALMENTE (sem serviço de terceiros) + copia-e-cola. */
import { toast } from 'sonner'
import { Copy } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field'
import { Textarea } from '@/components/ui/textarea'
import { QrCodeSvg } from '@/features/auth/components/QrCodeSvg'
import { formatCents, formatDateTime } from '@/lib/format'

export function PixPayment({ code, amountCents, expiresAt }: { code: string; amountCents: number; expiresAt: string | null }) {
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code)
      toast.success('Código PIX copiado.')
    } catch {
      toast.error('Não foi possível copiar. Selecione o código e copie manualmente.')
    }
  }
  return (
    <section aria-label="Pagamento PIX" className="flex flex-col items-start gap-3">
      <h3 className="text-title-card text-text">Pague com PIX</h3>
      <QrCodeSvg value={code} label={`QR Code PIX de ${formatCents(amountCents)}`} />
      <Field>
        <FieldLabel>PIX copia e cola</FieldLabel>
        <Textarea readOnly rows={3} value={code} className="font-mono text-xs" />
        <FieldDescription>Válido até {formatDateTime(expiresAt)}. A confirmação chega automaticamente.</FieldDescription>
      </Field>
      <Button variant="secondary" onClick={() => void copy()}>
        <Copy aria-hidden="true" />
        Copiar código
      </Button>
    </section>
  )
}
