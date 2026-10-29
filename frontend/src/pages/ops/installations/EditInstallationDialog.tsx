/**
 * Planejamento da instalação antes da conclusão — `PATCH /installations/{id}` (W-ORG §16):
 * vincular/trocar/desvincular a tela, trocar o técnico (notificado), data e observações.
 * Só envia o que mudou. Trocar a tela apaga o teste de conexão/dispensa anteriores (aviso explícito).
 * Erros do contrato: 409 INSTALLATION_INVALID_STATE · 404 (tela de outro estabelecimento) ·
 * 409 SCREEN_DECOMMISSIONED · 400 TECHNICIAN_OUT_OF_SCOPE / TECHNICIAN_REQUIRED.
 */
import * as React from 'react'
import { useMutation } from '@tanstack/react-query'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Field, FieldDescription, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { ScreenPicker, TechnicianSelect } from '@/components/ops/pickers'
import { installationsApi } from '@/pages/ops/api/client'
import type { Installation } from '@/pages/ops/api/types'
import { installationPatchBody, installationPatchError } from './installationRules'

export function EditInstallationDialog({
  inst,
  open,
  onOpenChange,
  onSaved,
}: {
  inst: Installation
  open: boolean
  onOpenChange: (open: boolean) => void
  onSaved: (inst: Installation) => void
}) {
  const [screenId, setScreenId] = React.useState<string | null>(inst.screen_id)
  const [techId, setTechId] = React.useState<string | null>(inst.technician_user_id)
  const [date, setDate] = React.useState(inst.scheduled_date ?? '')
  const [notes, setNotes] = React.useState(inst.notes ?? '')
  React.useEffect(() => {
    if (open) {
      setScreenId(inst.screen_id)
      setTechId(inst.technician_user_id)
      setDate(inst.scheduled_date ?? '')
      setNotes(inst.notes ?? '')
    }
  }, [open, inst.screen_id, inst.technician_user_id, inst.scheduled_date, inst.notes])

  const body = installationPatchBody(inst, { screenId, techId, date, notes })
  const changed = Object.keys(body).length > 0
  const screenChanged = 'screen_id' in body
  const losesTest = screenChanged && (!!inst.connection_test || !!inst.connection_test_waiver)
  const m = useMutation({
    mutationFn: () => installationsApi.update(inst.id, body),
    onSuccess: (r) => {
      onSaved(r)
      onOpenChange(false)
    },
  })
  const resetMutation = m.reset
  React.useEffect(() => {
    if (!open) resetMutation()
  }, [open, resetMutation])

  return (
    <Dialog open={open} onOpenChange={(o) => (!m.isPending ? onOpenChange(o) : undefined)}>
      <DialogContent size="md">
        <form
          noValidate
          className="flex flex-col gap-5"
          onSubmit={(e) => {
            e.preventDefault()
            if (changed) m.mutate()
          }}
        >
          <DialogHeader>
            <DialogTitle>Editar agendamento</DialogTitle>
            <DialogDescription>Tela, técnico, data e observações podem mudar até a conclusão.</DialogDescription>
          </DialogHeader>
          <ScreenPicker
            value={screenId}
            onChange={setScreenId}
            establishmentId={inst.establishment_id}
            legend="Tela"
            description="A conclusão exige a tela vinculada (somente telas deste estabelecimento)."
          />
          {screenId ? (
            <Button type="button" variant="tertiary" className="w-fit" onClick={() => setScreenId(null)} disabled={m.isPending}>
              Desvincular tela
            </Button>
          ) : null}
          {losesTest ? (
            <Alert variant="warning">
              <AlertDescription className="text-text">
                Trocar a tela apaga o teste de conexão e a dispensa registrados para a tela anterior; teste de novo depois.
              </AlertDescription>
            </Alert>
          ) : null}
          <TechnicianSelect
            permission="equipment.manage"
            value={techId}
            onChange={setTechId}
            required
            establishmentId={inst.establishment_id}
            selectedLabel={inst.technician_name}
          />
          <Field>
            <FieldLabel>Data agendada</FieldLabel>
            <Input type="date" value={date} onChange={(e) => setDate(e.target.value)} />
          </Field>
          <Field>
            <FieldLabel>Observações</FieldLabel>
            <Textarea value={notes} onChange={(e) => setNotes(e.target.value)} maxLength={4000} rows={3} />
            <FieldDescription>Visíveis para a equipe RD e o técnico.</FieldDescription>
          </Field>
          {m.error ? (
            <Alert variant="danger">
              <AlertDescription className="text-text">{installationPatchError(m.error)}</AlertDescription>
            </Alert>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)} disabled={m.isPending}>
              Voltar
            </Button>
            <Button type="submit" loading={m.isPending} disabled={!changed}>
              Salvar alterações
            </Button>
          </DialogFooter>
          {!changed ? <p className="m-0 text-xs text-text-muted">Nenhuma alteração para salvar.</p> : null}
        </form>
      </DialogContent>
    </Dialog>
  )
}
