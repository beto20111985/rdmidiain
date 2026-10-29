/** Etapa 4 — Período e horários (validação prévia; o servidor revalida e responde por campo). */
import * as React from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { ScheduleEditor, type ScheduleValue } from '@/components/campaigns/schedule-editor'
import { qk } from '@/lib/queryKeys'
import { campaignsApi } from '../lib/api'
import { serverFieldErrors } from '../lib/rules'
import { scheduleBody, scheduleFrom, validateSchedule } from '../lib/schedule'
import { ServerError, StepActions, type StepProps } from './shared'

export function StepSchedule({ campaign, onSaved, readOnly }: StepProps) {
  const qc = useQueryClient()
  const [value, setValue] = React.useState<ScheduleValue | null>(campaign ? scheduleFrom(campaign) : null)
  const [errors, setErrors] = React.useState<Record<string, string>>({})
  const [serverError, setServerError] = React.useState<unknown>(null)
  const [saving, setSaving] = React.useState<'draft' | 'next' | null>(null)

  if (!campaign || !value) return <Alert variant="info"><AlertDescription>Salve a identificação primeiro.</AlertDescription></Alert>

  const save = async (advance: boolean) => {
    if (readOnly) return
    const e = validateSchedule(value)
    setErrors(e)
    if (Object.keys(e).length) return
    setServerError(null)
    setSaving(advance ? 'next' : 'draft')
    try {
      const recurring = value.period_type !== 'AVULSO'
      const saved = await campaignsApi.update(campaign.id, {
        ...scheduleBody(value),
        ...(recurring ? { auto_renew: value.auto_renew } : {}),
        expected_version: campaign.version,
      })
      qc.setQueryData(qk.detail('campaigns', campaign.id), saved)
      setValue(scheduleFrom(saved))
      toast.success('Período e horários salvos.')
      onSaved(saved, advance)
    } catch (err) {
      const fe = serverFieldErrors(err)
      if (Object.keys(fe).length) setErrors(fe)
      else setServerError(err)
    } finally {
      setSaving(null)
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <ScheduleEditor value={value} onChange={(p) => setValue((v) => (v ? { ...v, ...p } : v))} errors={errors} disabled={readOnly} />
      <ServerError error={serverError} />
      {!readOnly ? <StepActions saving={saving} onSave={(a) => void save(a)} nextLabel="Salvar e revisar" /> : null}
    </div>
  )
}
