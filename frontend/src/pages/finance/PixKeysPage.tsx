/**
 * Financeiro → Chaves PIX dos estabelecimentos (P001 §87; ETAPA2 #643–#646, #1306–#1311).
 * Só admin/suporte autorizado (`pix_keys.manage`) cadastra ou troca: a chave nova fica
 * BLOQUEADA para saques por 12 h, a anterior é revogada na hora, o proprietário é notificado
 * e tudo é auditado. O estabelecimento apenas visualiza (mascarada).
 */
import * as React from 'react'
import { Controller, useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { KeyRound, ShieldAlert } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from '@/components/ui/field'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Skeleton } from '@/components/ui/skeleton'
import { ConfirmDialog } from '@/components/patterns/confirm-dialog'
import { ReasonConfirmDialog } from '@/components/finance/reason-confirm-dialog'
import { isCpfOrCnpjShape, pixKeyFieldError, stripDocumentMask } from '@/components/finance/payment-errors'
import { EmptyState, ErrorState } from '@/components/patterns/states'
import { StatusPill } from '@/components/finance/status-pill'
import { PIX_KEY_STATUS, PIX_KEY_TYPES, pixKeyTypeLabel } from '@/components/finance/labels'
import { useEstablishmentNames } from '@/components/finance/hooks'
import { qk } from '@/lib/queryKeys'
import { errorMessage, isApiError } from '@/lib/errors'
import { formatDateTime } from '@/lib/format'
import { useSearchParams } from 'react-router-dom'
import { financeApi, type PixKey } from './api'

const schema = z.object({
  key_type: z.string().min(1, 'Escolha o tipo de chave.'),
  key_value: z.string().trim().min(3, 'Informe a chave.').max(120),
  holder_name: z.string().trim().min(3, 'Informe o nome do titular.').max(200),
  holder_document: z
    .string()
    .trim()
    // CNPJ pode ser alfanumérico (IN RFB 2.229/2024): só a máscara é removida; o servidor confere o DV.
    .refine((v) => isCpfOrCnpjShape(v), 'CPF (11 dígitos) ou CNPJ (14 posições, numérico ou alfanumérico) do titular.'),
  reason: z.string().trim().min(5, 'Informe o motivo (mínimo 5 caracteres).').max(500),
})
type Values = z.infer<typeof schema>

function NewKeyForm({ establishmentId, establishmentName, hasCurrent }: { establishmentId: string; establishmentName: string; hasCurrent: boolean }) {
  const qc = useQueryClient()
  const [error, setError] = React.useState<string | null>(null)
  const [confirm, setConfirm] = React.useState<Values | null>(null)
  const { register, control, handleSubmit, reset, setError: setFieldError, formState: { errors } } = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: { key_type: '', key_value: '', holder_name: '', holder_document: '', reason: '' },
  })
  const save = async (v: Values) => {
    setError(null)
    try {
      const k = await financeApi.setPixKey(establishmentId, { ...v, holder_document: stripDocumentMask(v.holder_document) })
      toast.success(`Chave cadastrada. Saques liberados a partir de ${formatDateTime(k.active_from)}. O proprietário foi notificado.`)
      reset()
      await qc.invalidateQueries({ queryKey: ['withdrawals'] })
    } catch (e) {
      const keyErr = pixKeyFieldError(e)
      if (keyErr) setFieldError('key_value', { message: keyErr })
      else if (isApiError(e) && e.code === 'PIX_HOLDER_DOCUMENT_INVALID') setFieldError('holder_document', { message: e.message })
      else setError(errorMessage(e))
      throw e
    }
  }
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>{hasCurrent ? 'Trocar chave PIX' : 'Cadastrar chave PIX'}</CardTitle>
          <CardDescription>{establishmentName}</CardDescription>
        </div>
      </CardHeader>
      <Alert variant="warning">
        <ShieldAlert aria-hidden="true" />
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <AlertTitle>Bloqueio de segurança de 12 horas</AlertTitle>
          <AlertDescription>
            A chave nova só libera saques 12 h após o cadastro{hasCurrent ? '; a chave atual é revogada imediatamente' : ''}. O proprietário é notificado e a alteração fica na auditoria. Confirme a titularidade antes de salvar.
          </AlertDescription>
        </div>
      </Alert>
      {error ? (
        <Alert variant="danger">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}
      <form onSubmit={handleSubmit((v) => setConfirm(v))} noValidate className="flex flex-col gap-5">
        <FieldGroup>
          <div className="grid grid-cols-1 gap-5 sm:grid-cols-2">
            <Field invalid={!!errors.key_type}>
              <FieldLabel required>Tipo de chave</FieldLabel>
              <Controller
                control={control}
                name="key_type"
                render={({ field }) => (
                  <Select value={field.value || undefined} onValueChange={field.onChange}>
                    <SelectTrigger>
                      <SelectValue placeholder="Selecione" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectGroup>
                        {PIX_KEY_TYPES.map((t) => (
                          <SelectItem key={t.value} value={t.value}>
                            {t.label}
                          </SelectItem>
                        ))}
                      </SelectGroup>
                    </SelectContent>
                  </Select>
                )}
              />
              <FieldError>{errors.key_type?.message}</FieldError>
            </Field>
            <Field invalid={!!errors.key_value}>
              <FieldLabel required>Chave</FieldLabel>
              <Input autoComplete="off" {...register('key_value')} />
              <FieldError>{errors.key_value?.message}</FieldError>
            </Field>
            <Field invalid={!!errors.holder_name}>
              <FieldLabel required>Nome do titular</FieldLabel>
              <Input autoComplete="off" {...register('holder_name')} />
              <FieldError>{errors.holder_name?.message}</FieldError>
            </Field>
            <Field invalid={!!errors.holder_document}>
              <FieldLabel required>CPF/CNPJ do titular</FieldLabel>
              <Input autoComplete="off" className="uppercase" {...register('holder_document')} />
              <FieldDescription>Guardado cifrado; exibido mascarado.</FieldDescription>
              <FieldError>{errors.holder_document?.message}</FieldError>
            </Field>
          </div>
          <Field invalid={!!errors.reason}>
            <FieldLabel required>Motivo da alteração</FieldLabel>
            <Textarea rows={2} {...register('reason')} placeholder="ex.: solicitado pelo proprietário no chamado CH-000123" />
            <FieldError>{errors.reason?.message}</FieldError>
          </Field>
        </FieldGroup>
        <Button type="submit" className="self-start">
          <KeyRound aria-hidden="true" />
          {hasCurrent ? 'Trocar chave' : 'Cadastrar chave'}
        </Button>
      </form>
      <ConfirmDialog
        open={!!confirm}
        onOpenChange={(o) => !o && setConfirm(null)}
        title={hasCurrent ? 'Trocar a chave PIX deste estabelecimento?' : 'Cadastrar esta chave PIX?'}
        description={confirm ? `${pixKeyTypeLabel(confirm.key_type)} de ${confirm.holder_name} para ${establishmentName}.` : undefined}
        consequences={[
          'Saques ficam bloqueados por 12 horas.',
          ...(hasCurrent ? ['A chave atual é revogada imediatamente.'] : []),
          'O proprietário do estabelecimento é notificado.',
        ]}
        confirmLabel={hasCurrent ? 'Trocar chave' : 'Cadastrar chave'}
        onConfirm={async () => {
          if (confirm) await save(confirm)
        }}
      />
    </Card>
  )
}

export default function PixKeysPage() {
  const qc = useQueryClient()
  const ests = useEstablishmentNames()
  const [sp, setSp] = useSearchParams()
  const estId = sp.get('estabelecimento')
  const [revoking, setRevoking] = React.useState<PixKey | null>(null)
  const keys = useQuery({
    queryKey: qk.sub('withdrawals', 'pix-keys', estId ?? ''),
    queryFn: () => financeApi.pixKeys(estId as string),
    enabled: !!estId,
  })
  const current = (keys.data?.items ?? []).find((k) => k.status !== 'REVOKED') ?? null
  const estName = estId ? ests.nameOf(estId) : ''

  return (
    <div className="flex flex-col gap-6">
      <PageHeader title="Chaves PIX" description="Chaves de recebimento dos estabelecimentos" breadcrumbs={[{ label: 'Financeiro', to: '/financeiro' }, { label: 'Chaves PIX' }]} />
      <Field className="w-full max-w-md">
        <FieldLabel>Estabelecimento</FieldLabel>
        <Select
          value={estId ?? undefined}
          onValueChange={(v) =>
            setSp(
              (prev) => {
                const n = new URLSearchParams(prev)
                n.set('estabelecimento', v)
                return n
              },
              { replace: true },
            )
          }
        >
          <SelectTrigger>
            <SelectValue placeholder="Selecione o estabelecimento" />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              {ests.items.map((e) => (
                <SelectItem key={e.id} value={e.id}>
                  {e.trade_name || e.name}
                </SelectItem>
              ))}
            </SelectGroup>
          </SelectContent>
        </Select>
      </Field>
      {!estId ? (
        <EmptyState icon={KeyRound} title="Escolha um estabelecimento" description="Veja a chave ativa, o histórico e cadastre ou troque a chave de recebimento." />
      ) : (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-12 xl:gap-6">
          <Card className="xl:col-span-6">
            <CardHeader>
              <div className="flex flex-col gap-1">
                <CardTitle>Chaves cadastradas</CardTitle>
                <CardDescription>Mais recente primeiro. Valores sempre mascarados.</CardDescription>
              </div>
            </CardHeader>
            {keys.isLoading ? (
              <Skeleton className="h-32 w-full" />
            ) : keys.error ? (
              <ErrorState error={keys.error} onRetry={() => void keys.refetch()} size="compact" />
            ) : !keys.data?.items.length ? (
              <EmptyState size="compact" headingLevel="h3" title="Nenhuma chave cadastrada" description="Sem chave ativa o estabelecimento não consegue sacar." />
            ) : (
              <Table>
                <caption className="sr-only">Chaves PIX de {estName}</caption>
                <TableHeader>
                  <TableRow className="h-11 hover:bg-transparent">
                    <TableHead scope="col">Chave</TableHead>
                    <TableHead scope="col">Titular</TableHead>
                    <TableHead scope="col">Situação</TableHead>
                    <TableHead scope="col" className="text-right">
                      Ações
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {keys.data.items.map((k) => (
                    <TableRow key={k.id}>
                      <TableCell>
                        <div className="flex flex-col">
                          <span className="font-mono text-sm">{k.key_masked}</span>
                          <span className="text-xs text-text-muted">{pixKeyTypeLabel(k.key_type)} · cadastrada {formatDateTime(k.created_at)}</span>
                        </div>
                      </TableCell>
                      <TableCell>
                        <div className="flex flex-col">
                          <span>{k.holder_name}</span>
                          <span className="font-mono text-xs text-text-muted">{k.holder_document_masked}</span>
                        </div>
                      </TableCell>
                      <TableCell>
                        <div className="flex flex-col gap-1">
                          <StatusPill map={PIX_KEY_STATUS} value={k.status} />
                          <span className="text-xs text-text-muted">
                            {k.status === 'REVOKED' ? `Revogada ${formatDateTime(k.revoked_at)}` : k.status === 'COOLDOWN' ? `Libera saques em ${formatDateTime(k.active_from)}` : `Ativa desde ${formatDateTime(k.active_from)}`}
                          </span>
                        </div>
                      </TableCell>
                      <TableCell className="text-right">
                        {k.status !== 'REVOKED' ? (
                          <Button variant="destructive" onClick={() => setRevoking(k)} aria-label={`Revogar chave ${k.key_masked}`}>
                            Revogar
                          </Button>
                        ) : null}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </Card>
          <div className="xl:col-span-6">
            <NewKeyForm establishmentId={estId} establishmentName={estName} hasCurrent={!!current} />
          </div>
        </div>
      )}
      <RevokeDialog
        pixKey={revoking}
        onOpenChange={(o) => !o && setRevoking(null)}
        onDone={async () => {
          toast.success('Chave revogada. Saques ficam bloqueados até uma nova chave ser liberada.')
          await qc.invalidateQueries({ queryKey: ['withdrawals'] })
        }}
      />
    </div>
  )
}

function RevokeDialog({ pixKey, onOpenChange, onDone }: { pixKey: PixKey | null; onOpenChange: (o: boolean) => void; onDone: () => Promise<void> }) {
  return (
    <ReasonConfirmDialog
      open={!!pixKey}
      onOpenChange={onOpenChange}
      title="Revogar esta chave PIX?"
      description={pixKey ? `${pixKeyTypeLabel(pixKey.key_type)} ${pixKey.key_masked}.` : undefined}
      consequences={['O estabelecimento fica sem chave ativa e não consegue sacar.', 'A revogação fica na auditoria.']}
      requireText={pixKey?.key_masked}
      reasonLabel="Motivo da revogação"
      confirmLabel="Revogar chave"
      onConfirm={async (reason) => {
        if (!pixKey) return
        await financeApi.revokePixKey(pixKey.id, reason)
        await onDone()
      }}
    />
  )
}
