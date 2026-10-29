/**
 * Pedido de retirada de anúncio pelo ESTABELECIMENTO, a partir do código RD-XXXXXX exibido na
 * TV. Abre o formulário de chamado do módulo de Suporte (FE-ADM) já preenchido: categoria
 * RETIRADA_ANUNCIO + código + tela. Não mostra nenhum dado comercial do anunciante.
 */
import * as React from 'react'
import { useNavigate } from 'react-router-dom'
import { Flag } from 'lucide-react'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { CREATIVE_CODE_RE } from './utils'

export function RemovalRequestButton({ screenId, defaultCode, variant = 'secondary' }: { screenId?: string; defaultCode?: string; variant?: 'secondary' | 'tertiary' }) {
  const navigate = useNavigate()
  const [open, setOpen] = React.useState(false)
  const [code, setCode] = React.useState(defaultCode ?? '')
  const [touched, setTouched] = React.useState(false)
  const normalized = code.trim().toUpperCase()
  const valid = CREATIVE_CODE_RE.test(normalized)

  return (
    <>
      <Button variant={variant} onClick={() => { setCode(defaultCode ?? ''); setTouched(false); setOpen(true) }}>
        <Flag aria-hidden="true" />
        Pedir retirada de anúncio
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <form
            noValidate
            className="flex flex-col gap-5"
            onSubmit={(e) => {
              e.preventDefault()
              setTouched(true)
              if (!valid) return
              const sp = new URLSearchParams({ novo: '1', categoria: 'RETIRADA_ANUNCIO', codigo: normalized })
              if (screenId) sp.set('tela', screenId)
              setOpen(false)
              navigate(`/unidade/suporte?${sp.toString()}`)
            }}
          >
            <DialogHeader>
              <DialogTitle>Pedir retirada de anúncio</DialogTitle>
              <DialogDescription>
                Informe o código do anúncio exibido no canto da TV. A equipe RD analisa o pedido pelo Suporte.
              </DialogDescription>
            </DialogHeader>
            <Field invalid={touched && !valid} required>
              <FieldLabel>Código do anúncio</FieldLabel>
              <Input value={code} onChange={(e) => setCode(e.target.value.toUpperCase())} placeholder="RD-ABC234" autoComplete="off" spellCheck={false} />
              <FieldDescription>Formato RD-XXXXXX.</FieldDescription>
              <FieldError>{touched && !valid ? 'Código inválido. Use o formato RD-XXXXXX exibido na TV.' : null}</FieldError>
            </Field>
            <DialogFooter>
              <Button type="button" variant="secondary" onClick={() => setOpen(false)}>
                Cancelar
              </Button>
              <Button type="submit">Continuar no Suporte</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  )
}
