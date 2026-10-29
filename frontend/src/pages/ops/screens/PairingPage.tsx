/**
 * Parear tela — destino do QR Code exibido pelo player: `/telas/parear?code=XXXX-XXXX`
 * (W-PLAY `panel_pair_url`). Escolha da tela dentro do escopo do usuário, confirmação e
 * acompanhamento em tempo real (evento screen.status) até o player conectar.
 * Backend: POST /screens/{screen_id}/pairing {pairing_code} (screens.pair no escopo da tela).
 */
import * as React from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useMutation } from '@tanstack/react-query'
import { CheckCircle2, Loader2, QrCode, TriangleAlert } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { EstablishmentPicker, ScreenPicker } from '@/components/ops/pickers'
import { PresenceBadge } from '@/components/ops/presence-badge'
import { maskPairingInput, normalizePairingCode } from '@/components/ops/utils'
import { isApiError } from '@/lib/errors'
import { screensApi, presenceToSummary } from '@/pages/ops/api/client'
import { useScreenLookup, useScreenPresence } from '@/pages/ops/api/hooks'
import type { Device } from '@/pages/ops/api/types'

const CRUMBS = [{ label: 'Telas', to: '/telas' }, { label: 'Parear tela' }]

function pairingErrorText(e: unknown): { title: string; text: string } {
  if (!isApiError(e)) return { title: 'Não foi possível parear', text: 'Tente novamente.' }
  switch (e.code) {
    case 'PAIRING_CODE_NOT_FOUND':
      return { title: 'Código não encontrado', text: 'Confira o código exibido na TV. Se a tela do player mudou, use o código novo.' }
    case 'PAIRING_CODE_EXPIRED':
      return { title: 'Código expirado', text: 'O código vale 15 minutos. Gere um novo no player e tente de novo.' }
    case 'PAIRING_ALREADY_CLAIMED':
      return { title: 'Código já utilizado', text: e.message }
    case 'PAIRING_CODE_INVALID':
      return { title: 'Código inválido', text: 'O código tem 8 caracteres no formato XXXX-XXXX.' }
    case 'SCREEN_DECOMMISSIONED':
      return { title: 'Tela retirada', text: 'Esta tela foi retirada definitivamente e não aceita pareamento.' }
    default:
      return { title: 'Não foi possível parear', text: e.message }
  }
}

export default function PairingPage() {
  const [sp] = useSearchParams()
  const [code, setCode] = React.useState(() => maskPairingInput(sp.get('code') ?? ''))
  const [estId, setEstId] = React.useState<string | null>(null)
  const [screenId, setScreenId] = React.useState<string | null>(sp.get('screen_id'))
  const [touched, setTouched] = React.useState(false)
  const [paired, setPaired] = React.useState<{ device: Device; screenId: string; revoked: number } | null>(null)
  const preset = useScreenLookup(sp.get('screen_id'))
  const current = useScreenPresence(screenId ?? undefined)
  const live = useScreenPresence(paired?.screenId)
  const normalized = normalizePairingCode(code)

  React.useEffect(() => {
    if (preset.data && !estId) setEstId(preset.data.establishment_id)
  }, [preset.data, estId])

  const m = useMutation({
    mutationFn: () => screensApi.pair(screenId!, normalized!),
    onSuccess: (r) => setPaired({ device: r.device, screenId: screenId!, revoked: r.revoked_device_ids.length }),
  })

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    setTouched(true)
    if (!normalized || !screenId) return
    m.mutate()
  }

  if (paired) {
    const s = live.data ? presenceToSummary(live.data) : undefined
    const connected = !!s && s.status !== 'offline'
    return (
      <div className="flex flex-col gap-6">
        <PageHeader title="Parear tela" breadcrumbs={CRUMBS} />
        <Card className="max-w-2xl">
          <CardHeader>
            <CardTitle>{connected ? 'Player conectado' : 'Pareamento confirmado'}</CardTitle>
            <PresenceBadge presence={s} loading={live.isLoading} />
          </CardHeader>
          <div role="status" aria-live="polite" className="flex items-start gap-3">
            {connected ? (
              <CheckCircle2 aria-hidden="true" className="mt-0.5 size-6 shrink-0 text-success" />
            ) : (
              <Loader2 aria-hidden="true" className="mt-0.5 size-6 shrink-0 animate-spin text-link" />
            )}
            <p className="m-0 text-sm text-text">
              {connected
                ? 'O player autenticou e está se comunicando com o servidor. Ele baixará a programação automaticamente.'
                : 'Aguardando o player concluir a autenticação e conectar. Isso costuma levar alguns segundos; esta página atualiza sozinha.'}
            </p>
          </div>
          {paired.revoked ? (
            <Alert variant="warning">
              <AlertDescription className="text-text">O dispositivo anterior desta tela foi revogado e desconectado.</AlertDescription>
            </Alert>
          ) : null}
          <div className="flex flex-wrap gap-2">
            <Button asChild>
              <Link to={`/telas/${paired.screenId}`}>Ver a tela</Link>
            </Button>
            <Button
              variant="secondary"
              onClick={() => {
                setPaired(null)
                setCode('')
                setScreenId(null)
                setTouched(false)
                m.reset()
              }}
            >
              Parear outra tela
            </Button>
          </div>
        </Card>
      </div>
    )
  }

  const err = m.error ? pairingErrorText(m.error) : null
  const hasActiveDevice = !!current.data?.paired

  return (
    <div className="flex flex-col gap-6">
      <PageHeader title="Parear tela" description="Vincule o player instalado na TV a uma tela cadastrada." breadcrumbs={CRUMBS} />
      <form noValidate onSubmit={submit} className="flex max-w-3xl flex-col gap-6">
        <Card>
          <CardHeader>
            <div className="flex flex-col gap-1">
              <CardTitle>1. Código exibido na TV</CardTitle>
              <CardDescription>Ao abrir o player sem pareamento, a TV mostra um QR Code e um código XXXX-XXXX (válido por 15 min).</CardDescription>
            </div>
            <QrCode aria-hidden="true" className="size-8 text-link" />
          </CardHeader>
          <Field invalid={touched && !normalized} required className="max-w-xs">
            <FieldLabel>Código de pareamento</FieldLabel>
            <Input
              value={code}
              onChange={(e) => setCode(maskPairingInput(e.target.value))}
              autoComplete="off"
              autoCapitalize="characters"
              spellCheck={false}
              className="text-lg font-semibold tracking-[0.2em] tabular-nums"
              placeholder="ABCD-2345"
            />
            <FieldDescription>Letras e números sem 0, O, 1, I, L, K, W e X.</FieldDescription>
            <FieldError>{touched && !normalized ? 'Digite os 8 caracteres do código exibido na TV.' : null}</FieldError>
          </Field>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>2. Tela que receberá o player</CardTitle>
          </CardHeader>
          <EstablishmentPicker
            value={estId}
            onChange={(id) => {
              setEstId(id)
              setScreenId(null)
            }}
            onlyActive
            legend="Estabelecimento (filtro)"
          />
          <ScreenPicker
            value={screenId}
            onChange={setScreenId}
            establishmentId={estId}
            required
            error={touched && !screenId ? 'Selecione a tela.' : null}
          />
          {screenId && hasActiveDevice ? (
            <Alert variant="warning">
              <TriangleAlert aria-hidden="true" />
              <div>
                <AlertTitle>Esta tela já tem um player pareado</AlertTitle>
                <AlertDescription>Ao confirmar, o dispositivo atual será revogado e desconectado imediatamente.</AlertDescription>
              </div>
            </Alert>
          ) : null}
        </Card>

        {err ? (
          <Alert variant="danger">
            <AlertTitle>{err.title}</AlertTitle>
            <AlertDescription className="text-text">{err.text}</AlertDescription>
          </Alert>
        ) : null}

        <div className="flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
          <Button asChild variant="secondary">
            <Link to="/telas">Cancelar</Link>
          </Button>
          <Button type="submit" loading={m.isPending} loadingText="Pareando…">
            {hasActiveDevice ? 'Substituir player e parear' : 'Confirmar pareamento'}
          </Button>
        </div>
      </form>
    </div>
  )
}
