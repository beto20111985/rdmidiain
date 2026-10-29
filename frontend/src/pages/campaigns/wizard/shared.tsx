/** Peças comuns às etapas do wizard de campanha. */
import * as React from 'react'
import { Save } from 'lucide-react'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { isApiError } from '@/lib/errors'
import type { CampaignDetail } from '../lib/types'
import type { useCampaignArea } from '../lib/hooks'
import { describeError } from '@/pages/campaigns/lib/errorText'

export interface StepProps {
  campaign: CampaignDetail | undefined
  area: ReturnType<typeof useCampaignArea>
  /** Após salvar: `advance` vai para a próxima etapa. */
  onSaved: (c: CampaignDetail | undefined, advance: boolean) => void
  readOnly: boolean
}

export function ServerError({ error, title = 'Não foi possível salvar' }: { error: unknown; title?: string }) {
  if (!error) return null
  return (
    <Alert variant="danger">
      <div>
        <AlertTitle>{title}</AlertTitle>
        <AlertDescription>
          {describeError(error)}
          {isApiError(error) && error.requestId ? <span className="block text-xs text-text-muted">Código para o suporte: {error.requestId}</span> : null}
        </AlertDescription>
      </div>
    </Alert>
  )
}

/** "Salvar rascunho" (fica na etapa) + "Salvar e continuar" (avança). */
export function StepActions({
  saving,
  onSave,
  disabled,
  nextLabel = 'Salvar e continuar',
  extra,
}: {
  saving: 'draft' | 'next' | null
  onSave: (advance: boolean) => void
  disabled?: boolean
  nextLabel?: string
  extra?: React.ReactNode
}) {
  return (
    <div className="flex flex-wrap items-center justify-end gap-3 border-t border-border pt-4">
      {extra}
      <Button variant="secondary" onClick={() => onSave(false)} loading={saving === 'draft'} disabled={disabled || saving !== null}>
        <Save aria-hidden="true" />
        Salvar rascunho
      </Button>
      <Button onClick={() => onSave(true)} loading={saving === 'next'} disabled={disabled || saving !== null}>
        {nextLabel}
      </Button>
    </div>
  )
}
