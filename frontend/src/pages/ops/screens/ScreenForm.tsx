/**
 * Formulário de tela (criar/editar). O código TEL-UF-XXXX é gerado pelo servidor e é imutável;
 * troca de estabelecimento, status e retirada têm ações próprias no detalhe (com motivo).
 */
import * as React from 'react'
import { useNavigate } from 'react-router-dom'
import { Controller, useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { OptionSelect } from '@/components/ops/filter-select'
import { EstablishmentPicker } from '@/components/ops/pickers'
import { screensApi } from '@/pages/ops/api/client'
import { applyServerErrors } from '@/pages/ops/api/form-errors'
import {
  CLASS_LABEL,
  DISPLAY_TYPE_LABEL,
  ORIENTATION_LABEL,
  PLAYER_TYPE_LABEL,
  PROGRAMMING_MODE_LABEL,
  TV_OWNER_LABEL,
} from '@/pages/ops/api/labels'
import type { Screen } from '@/pages/ops/api/types'
import { SCREEN_FIELDS, screenFormToBody, screenSchema, screenToForm, type ScreenFormValues } from './screenFormModel'

const opts = (m: Record<string, string>) => Object.entries(m).map(([value, label]) => ({ value, label }))

export function ScreenForm({ screen, establishmentId }: { screen: Screen | null; establishmentId?: string | null }) {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const isEdit = !!screen
  const [general, setGeneral] = React.useState<string | null>(null)
  const {
    register,
    control,
    handleSubmit,
    setError,
    formState: { errors, isSubmitting },
  } = useForm<ScreenFormValues>({ resolver: zodResolver(screenSchema), defaultValues: screenToForm(screen, establishmentId) })

  const m = useMutation({
    mutationFn: (v: ScreenFormValues) => (isEdit ? screensApi.update(screen!.id, screenFormToBody(v)) : screensApi.create(screenFormToBody(v))),
    onSuccess: (s) => {
      void qc.invalidateQueries({ queryKey: ['screens'] })
      void qc.invalidateQueries({ queryKey: ['establishments'] })
      toast.success(isEdit ? 'Tela atualizada.' : `Tela ${s.friendly_id} cadastrada. Agora pareie o player.`)
      navigate(`/telas/${s.id}`)
    },
  })

  const onSubmit = async (v: ScreenFormValues) => {
    setGeneral(null)
    try {
      await m.mutateAsync(v)
    } catch (err) {
      setGeneral(
        applyServerErrors(err, setError, {
          fields: SCREEN_FIELDS,
          codeToField: { ESTABLISHMENT_INACTIVE: 'establishment_id', EQUIPMENT_DUPLICATE: 'serial_number' },
        }),
      )
    }
  }

  const select = (name: 'orientation' | 'display_type' | 'player_type' | 'tv_owner' | 'screen_class' | 'programming_mode', options: Array<{ value: string; label: string }>) => (
    <Controller
      control={control}
      name={name}
      render={({ field }) => <OptionSelect value={field.value} onChange={(v) => v && field.onChange(v)} options={options} />}
    />
  )

  return (
    <form noValidate onSubmit={handleSubmit(onSubmit)} className="flex flex-col gap-6">
      {general ? (
        <Alert variant="danger">
          <AlertDescription className="text-text">{general}</AlertDescription>
        </Alert>
      ) : null}
      <Card>
        <CardHeader>
          <CardTitle>Identificação</CardTitle>
          {isEdit ? <CardDescription>Código {screen!.friendly_id} (imutável).</CardDescription> : null}
        </CardHeader>
        <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
          {!isEdit ? (
            <div className="md:col-span-2">
              <Controller
                control={control}
                name="establishment_id"
                render={({ field }) => (
                  <EstablishmentPicker
                    required
                    onlyActive
                    value={field.value}
                    onChange={(id) => field.onChange(id)}
                    error={errors.establishment_id?.message}
                    description="O código TEL-UF-XXXX usa a UF do estabelecimento."
                  />
                )}
              />
            </div>
          ) : null}
          <Field invalid={!!errors.name} required>
            <FieldLabel>Nome da tela</FieldLabel>
            <Input {...register('name')} placeholder="Ex.: Caixa principal" />
            <FieldError>{errors.name?.message}</FieldError>
          </Field>
          <Field>
            <FieldLabel>Orientação</FieldLabel>
            {select('orientation', opts(ORIENTATION_LABEL))}
          </Field>
          <Field>
            <FieldLabel>Classe</FieldLabel>
            {select('screen_class', opts(CLASS_LABEL))}
          </Field>
          <Field invalid={!!errors.weight}>
            <FieldLabel>Peso</FieldLabel>
            <Input {...register('weight')} inputMode="decimal" />
            <FieldDescription>1 = padrão (rateio de comissão por peso).</FieldDescription>
            <FieldError>{errors.weight?.message}</FieldError>
          </Field>
          <Field>
            <FieldLabel>Programação</FieldLabel>
            {select('programming_mode', opts(PROGRAMMING_MODE_LABEL))}
          </Field>
        </div>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>TV / painel</CardTitle>
        </CardHeader>
        <div className="grid grid-cols-1 gap-5 md:grid-cols-2 xl:grid-cols-3">
          <Field>
            <FieldLabel>Tipo de display</FieldLabel>
            {select('display_type', opts(DISPLAY_TYPE_LABEL))}
          </Field>
          <Field>
            <FieldLabel>Propriedade da TV</FieldLabel>
            {select('tv_owner', opts(TV_OWNER_LABEL))}
          </Field>
          <Field invalid={!!errors.hardware_brand}>
            <FieldLabel>Marca</FieldLabel>
            <Input {...register('hardware_brand')} />
            <FieldError>{errors.hardware_brand?.message}</FieldError>
          </Field>
          <Field invalid={!!errors.hardware_model}>
            <FieldLabel>Modelo</FieldLabel>
            <Input {...register('hardware_model')} />
            <FieldError>{errors.hardware_model?.message}</FieldError>
          </Field>
          <Field invalid={!!errors.inches}>
            <FieldLabel>Polegadas</FieldLabel>
            <Input {...register('inches')} inputMode="numeric" />
            <FieldError>{errors.inches?.message}</FieldError>
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field invalid={!!errors.resolution_w}>
              <FieldLabel>Largura (px)</FieldLabel>
              <Input {...register('resolution_w')} inputMode="numeric" />
              <FieldError>{errors.resolution_w?.message}</FieldError>
            </Field>
            <Field invalid={!!errors.resolution_h}>
              <FieldLabel>Altura (px)</FieldLabel>
              <Input {...register('resolution_h')} inputMode="numeric" />
              <FieldError>{errors.resolution_h?.message}</FieldError>
            </Field>
          </div>
          <Field invalid={!!errors.serial_number}>
            <FieldLabel>Nº de série da TV</FieldLabel>
            <Input {...register('serial_number')} autoComplete="off" />
            <FieldError>{errors.serial_number?.message}</FieldError>
          </Field>
          <Field invalid={!!errors.asset_tag}>
            <FieldLabel>Patrimônio</FieldLabel>
            <Input {...register('asset_tag')} autoComplete="off" />
            <FieldError>{errors.asset_tag?.message}</FieldError>
          </Field>
        </div>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Player (TV Box / app)</CardTitle>
          <CardDescription>Troca física de TV ou TV Box com histórico: use “Trocar hardware” no detalhe da tela.</CardDescription>
        </CardHeader>
        <div className="grid grid-cols-1 gap-5 md:grid-cols-2 xl:grid-cols-4">
          <Field>
            <FieldLabel>Tipo de player</FieldLabel>
            {select('player_type', opts(PLAYER_TYPE_LABEL))}
          </Field>
          <Field invalid={!!errors.player_brand}>
            <FieldLabel>Marca</FieldLabel>
            <Input {...register('player_brand')} />
            <FieldError>{errors.player_brand?.message}</FieldError>
          </Field>
          <Field invalid={!!errors.player_model}>
            <FieldLabel>Modelo</FieldLabel>
            <Input {...register('player_model')} />
            <FieldError>{errors.player_model?.message}</FieldError>
          </Field>
          <Field invalid={!!errors.player_serial_number}>
            <FieldLabel>Nº de série</FieldLabel>
            <Input {...register('player_serial_number')} autoComplete="off" />
            <FieldError>{errors.player_serial_number?.message}</FieldError>
          </Field>
        </div>
      </Card>

      <Card>
        <Field invalid={!!errors.notes}>
          <FieldLabel>Observações</FieldLabel>
          <Textarea {...register('notes')} rows={3} />
          <FieldError>{errors.notes?.message}</FieldError>
        </Field>
        {isEdit ? (
          <Field invalid={!!errors.reason}>
            <FieldLabel>Motivo da alteração</FieldLabel>
            <Textarea {...register('reason')} rows={2} />
            <FieldDescription>Recomendado ao alterar hardware ou orientação (vai para o histórico da tela).</FieldDescription>
            <FieldError>{errors.reason?.message}</FieldError>
          </Field>
        ) : null}
      </Card>

      <div className="flex flex-col-reverse gap-3 sm:flex-row sm:justify-end">
        <Button type="button" variant="secondary" onClick={() => navigate(isEdit ? `/telas/${screen!.id}` : '/telas')}>
          Cancelar
        </Button>
        <Button type="submit" loading={isSubmitting} loadingText="Salvando…">
          {isEdit ? 'Salvar alterações' : 'Cadastrar tela'}
        </Button>
      </div>
    </form>
  )
}
