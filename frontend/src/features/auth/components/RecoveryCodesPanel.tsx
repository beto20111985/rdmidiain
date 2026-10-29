/**
 * Códigos de recuperação do 2FA — exibidos UMA única vez (SPEC AÇÃO 003 §11). Ficam só no estado
 * do componente: nada em localStorage/sessionStorage, nada em URL. Exige a confirmação de que
 * foram guardados antes de continuar.
 */
import * as React from 'react'
import { Check, Copy, Download } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Field, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'

export function RecoveryCodesPanel({
  codes,
  confirmLabel = 'Concluir',
  pending = false,
  onConfirm,
}: {
  codes: string[]
  confirmLabel?: string
  pending?: boolean
  onConfirm: () => void | Promise<void>
}) {
  const [saved, setSaved] = React.useState(false)
  const [copied, setCopied] = React.useState(false)
  const codesText = codes.join('\n')

  return (
    <div className="flex flex-col gap-6">
      <Alert variant="warning">
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <AlertTitle>Guarde estes códigos agora</AlertTitle>
          <AlertDescription className="text-text">
            Eles permitem entrar se você perder o celular. Cada código vale uma vez e eles não serão mostrados novamente.
          </AlertDescription>
        </div>
      </Alert>
      <ul aria-label="Códigos de recuperação" className="m-0 grid list-none grid-cols-2 gap-2 p-0 font-mono text-sm tabular-nums">
        {codes.map((c) => (
          <li key={c} className="rounded-md border border-border bg-field px-3 py-2 text-center text-text">
            {c}
          </li>
        ))}
      </ul>
      <div className="flex flex-wrap gap-2">
        <Button
          variant="secondary"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(codesText)
              setCopied(true)
            } catch {
              setCopied(false)
            }
          }}
        >
          {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
          {copied ? 'Copiados' : 'Copiar códigos'}
        </Button>
        <Button
          variant="secondary"
          onClick={() => {
            const blob = new Blob([`RD Mídia Indoor — códigos de recuperação\n\n${codesText}\n`], { type: 'text/plain' })
            const url = URL.createObjectURL(blob)
            const a = document.createElement('a')
            a.href = url
            a.download = 'rd-midia-indoor-codigos-recuperacao.txt'
            a.click()
            URL.revokeObjectURL(url)
          }}
        >
          <Download aria-hidden="true" />
          Baixar .txt
        </Button>
      </div>
      <Field orientation="horizontal" className="min-h-11">
        <Checkbox checked={saved} onCheckedChange={(v) => setSaved(v === true)} />
        <FieldLabel className="font-medium">Guardei os códigos em um local seguro</FieldLabel>
      </Field>
      <Button size="block" disabled={!saved} loading={pending} onClick={() => void onConfirm()}>
        {confirmLabel}
      </Button>
    </div>
  )
}
