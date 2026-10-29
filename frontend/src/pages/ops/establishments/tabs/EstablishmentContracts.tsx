/**
 * Aba "Contratos e comodato": aceites (versão, método, IP, data/hora, PDF), registro manual de
 * contrato em papel (RD) e acordos de comodato (criar/encerrar com motivo).
 */
import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { FileDown, FilePlus2, FileSignature, Plus } from 'lucide-react'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Skeleton } from '@/components/ui/skeleton'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { EmptyState, ErrorState } from '@/components/patterns/states'
import { OptionSelect } from '@/components/ops/filter-select'
import { ReasonDialog } from '@/components/ops/reason-dialog'
import { UserName, ScreenCode } from '@/components/ops/entity-name'
import { useCan } from '@/hooks/usePermissions'
import { qk } from '@/lib/queryKeys'
import { errorMessage } from '@/lib/errors'
import { formatBytes, formatCents, formatDateTime } from '@/lib/format'
import { comodatoApi, contractsApi, saveBlob } from '@/pages/ops/api/client'
import {
  ACCEPTANCE_METHOD_LABEL,
  CONTRACT_KIND_LABEL,
  PAID_BY_LABEL,
  UPGRADE_POLICY_LABEL,
  labelOf,
} from '@/pages/ops/api/labels'
import type { ComodatoAgreement, ContractAcceptance, Establishment } from '@/pages/ops/api/types'
import { ComodatoFormDialog } from '../../plans/ComodatoFormDialog'

export function AcceptanceList({
  items,
  timeZone,
}: {
  items: ContractAcceptance[]
  timeZone?: string
}) {
  const [downloading, setDownloading] = React.useState<string | null>(null)
  const download = async (a: ContractAcceptance) => {
    setDownloading(a.id)
    try {
      const { blob, filename } = await contractsApi.pdf(a.id)
      saveBlob(blob, filename ?? `contrato-${a.kind.toLowerCase()}-v${a.version}.pdf`)
    } catch (e) {
      toast.error(errorMessage(e))
    } finally {
      setDownloading(null)
    }
  }
  return (
    <ul className="m-0 flex list-none flex-col divide-y divide-border p-0">
      {items.map((a) => (
        <li key={a.id} className="flex flex-col gap-2 py-4 first:pt-0 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex min-w-0 flex-col gap-1">
            <span className="font-semibold text-text">
              {labelOf(CONTRACT_KIND_LABEL, a.kind)} · versão {a.version}
            </span>
            <span className="text-sm text-text-muted">
              {labelOf(ACCEPTANCE_METHOD_LABEL, a.method)} em {formatDateTime(a.accepted_at, timeZone)}
              {a.ip_address ? ` · IP ${a.ip_address}` : ''}
            </span>
            <span className="text-xs text-text-muted">
              {a.method === 'MANUAL_RECORD' ? (
                <>
                  Assinado por {a.signer_name ?? '—'} · registrado por <UserName id={a.recorded_by_user_id} />
                </>
              ) : (
                <>
                  Aceito por <UserName id={a.accepted_by_user_id} />
                </>
              )}
              {a.body_sha256 ? ` · texto SHA-256 ${a.body_sha256.slice(0, 12)}…` : ''}
            </span>
            {a.notes ? <span className="text-xs text-text-muted">Obs.: {a.notes}</span> : null}
          </div>
          {a.pdf ? (
            <Button variant="secondary" onClick={() => void download(a)} loading={downloading === a.id} loadingText="Baixando PDF…">
              <FileDown aria-hidden="true" />
              PDF ({formatBytes(a.pdf.size)})
            </Button>
          ) : (
            <span className="text-xs text-text-muted">PDF indisponível</span>
          )}
        </li>
      ))}
    </ul>
  )
}

export function EstablishmentContracts({ est }: { est: Establishment }) {
  const can = useCan()
  const qc = useQueryClient()
  const [manualOpen, setManualOpen] = React.useState(false)
  const [comodatoOpen, setComodatoOpen] = React.useState(false)
  const [ending, setEnding] = React.useState<ComodatoAgreement | null>(null)

  const acc = useQuery({
    queryKey: qk.sub('establishments', 'contracts', 'acceptances', est.id),
    queryFn: () => contractsApi.acceptances({ org_id: est.org_id, establishment_id: est.id, page_size: 50 }),
  })
  const com = useQuery({
    queryKey: qk.sub('establishments', 'comodato', 'by-establishment', est.id),
    queryFn: () => comodatoApi.list({ establishment_id: est.id, page_size: 50 }),
  })
  const end = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) => comodatoApi.end(id, reason),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['establishments'] })
      toast.success('Acordo de comodato encerrado.')
    },
  })

  return (
    <div className="grid grid-cols-1 gap-6">
      <Card>
        <CardHeader>
          <div className="flex flex-col gap-1">
            <CardTitle>Contratos aceitos</CardTitle>
            <CardDescription>O aceite eletrônico registra versão, usuário, IP e data/hora; o PDF é gerado pelo servidor.</CardDescription>
          </div>
          {can('organizations.manage') ? (
            <Button variant="secondary" onClick={() => setManualOpen(true)}>
              <FilePlus2 aria-hidden="true" />
              Registrar contrato em papel
            </Button>
          ) : null}
        </CardHeader>
        {acc.isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : acc.error ? (
          <ErrorState error={acc.error} onRetry={() => void acc.refetch()} size="compact" />
        ) : !acc.data?.items.length ? (
          <EmptyState
            icon={FileSignature}
            size="compact"
            headingLevel="h3"
            title="Nenhum contrato aceito ainda."
            description="O responsável do estabelecimento aceita o contrato em “Minha unidade → Contrato” após receber o convite de acesso."
          />
        ) : (
          <AcceptanceList items={acc.data.items} timeZone={est.timezone} />
        )}
      </Card>

      <Card>
        <CardHeader>
          <div className="flex flex-col gap-1">
            <CardTitle>Comodato</CardTitle>
            <CardDescription>TV da RD cedida ao estabelecimento: caução, instalação, upgrade e responsabilidades.</CardDescription>
          </div>
          {can('establishments.manage') && est.is_active ? (
            <Button variant="secondary" onClick={() => setComodatoOpen(true)}>
              <Plus aria-hidden="true" />
              Novo acordo
            </Button>
          ) : null}
        </CardHeader>
        {com.isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : com.error ? (
          <ErrorState error={com.error} onRetry={() => void com.refetch()} size="compact" />
        ) : !com.data?.items.length ? (
          <EmptyState size="compact" headingLevel="h3" title="Nenhum acordo de comodato." description="Estabelecimentos com TV própria não precisam de comodato." />
        ) : (
          <ul className="m-0 flex list-none flex-col divide-y divide-border p-0">
            {com.data.items.map((a) => (
              <li key={a.id} className="flex flex-col gap-2 py-4 first:pt-0 sm:flex-row sm:justify-between">
                <div className="flex flex-col gap-1 text-sm">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge tone={a.status === 'ACTIVE' ? 'success' : 'neutral'}>{a.status === 'ACTIVE' ? 'Ativo' : 'Encerrado'}</Badge>
                    <span className="text-text">Desde {formatDateTime(a.started_at, est.timezone)}</span>
                    {a.screen_id ? (
                      <span className="text-text-muted">
                        · Tela <ScreenCode id={a.screen_id} />
                      </span>
                    ) : null}
                  </div>
                  <span className="text-text-muted">
                    Caução: {a.deposit_required ? formatCents(a.deposit_cents) : 'não'} · Instalação:{' '}
                    {a.installation_fee_required ? formatCents(a.installation_fee_cents) : 'gratuita'} · Upgrade:{' '}
                    {labelOf(UPGRADE_POLICY_LABEL, a.upgrade_policy)}
                    {a.upgrade_policy === 'PAID' ? ` (${formatCents(a.upgrade_fee_cents)})` : ''}
                  </span>
                  <span className="text-xs text-text-muted">
                    Manutenção: {labelOf(PAID_BY_LABEL, a.maintenance_paid_by)} · Quebra: {labelOf(PAID_BY_LABEL, a.breakage_paid_by)}
                    {a.status === 'ENDED' ? ` · Encerrado em ${formatDateTime(a.ended_at, est.timezone)}: ${a.end_reason ?? '—'}` : ''}
                  </span>
                </div>
                {a.status === 'ACTIVE' && can('establishments.manage') ? (
                  <Button variant="destructive" onClick={() => setEnding(a)}>
                    Encerrar
                  </Button>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <ManualAcceptanceDialog open={manualOpen} onOpenChange={setManualOpen} est={est} agreements={com.data?.items ?? []} />
      <ComodatoFormDialog open={comodatoOpen} onOpenChange={setComodatoOpen} establishmentId={est.id} />
      <ReasonDialog
        open={!!ending}
        onOpenChange={(o) => !o && setEnding(null)}
        title="Encerrar acordo de comodato?"
        consequences={[
          'Só é possível encerrar depois que todos os equipamentos em comodato forem devolvidos (movimentação “Devolução” no Estoque).',
        ]}
        confirmLabel="Encerrar acordo"
        destructive
        onConfirm={(reason) => end.mutateAsync({ id: ending!.id, reason })}
      />
    </div>
  )
}

function ManualAcceptanceDialog({
  open,
  onOpenChange,
  est,
  agreements,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  est: Establishment
  agreements: ComodatoAgreement[]
}) {
  const qc = useQueryClient()
  const tpl = useQuery({
    queryKey: qk.sub('establishments', 'contracts', 'templates', 'published'),
    queryFn: () => contractsApi.templates({ status: 'PUBLISHED' }),
    enabled: open,
  })
  const [templateId, setTemplateId] = React.useState<string | null>(null)
  const [signer, setSigner] = React.useState('')
  const [notes, setNotes] = React.useState('')
  const [agreementId, setAgreementId] = React.useState<string | null>(null)
  const [touched, setTouched] = React.useState(false)
  React.useEffect(() => {
    if (open) {
      setTemplateId(null)
      setSigner('')
      setNotes('')
      setAgreementId(null)
      setTouched(false)
    }
  }, [open])
  const templates = (tpl.data?.items ?? []).filter((t) => t.kind === 'PARTNERSHIP' || t.kind === 'COMODATO')
  const selected = templates.find((t) => t.id === templateId)
  const needsAgreement = selected?.kind === 'COMODATO'
  const errs = {
    template: !templateId ? 'Selecione o contrato.' : null,
    signer: signer.trim().length < 3 ? 'Informe o nome de quem assinou.' : null,
    notes: notes.trim().length < 20 ? 'Descreva onde está o documento físico (mínimo 20 caracteres).' : null,
    agreement: needsAgreement && !agreementId ? 'Selecione o acordo de comodato.' : null,
  }
  const valid = !Object.values(errs).some(Boolean)
  const m = useMutation({
    mutationFn: () =>
      contractsApi.manual({
        template_id: templateId,
        org_id: est.org_id,
        establishment_id: est.id,
        comodato_agreement_id: needsAgreement ? agreementId : null,
        signer_name: signer.trim(),
        notes: notes.trim(),
      }),
    onSuccess: (r) => {
      void qc.invalidateQueries({ queryKey: ['establishments'] })
      toast.success(r.establishment_activated ? 'Contrato registrado. Estabelecimento ativado.' : 'Contrato registrado.')
      onOpenChange(false)
    },
  })
  return (
    <Dialog open={open} onOpenChange={(o) => (!m.isPending ? onOpenChange(o) : undefined)}>
      <DialogContent size="md">
        <form
          noValidate
          className="flex flex-col gap-5"
          onSubmit={(e) => {
            e.preventDefault()
            setTouched(true)
            if (valid) m.mutate()
          }}
        >
          <DialogHeader>
            <DialogTitle>Registrar contrato assinado em papel</DialogTitle>
            <DialogDescription>
              Use apenas quando o responsável assinou fisicamente. O registro é auditado e gera o PDF do aceite.
            </DialogDescription>
          </DialogHeader>
          <Field invalid={touched && !!errs.template} required>
            <FieldLabel>Contrato (versão publicada)</FieldLabel>
            <OptionSelect
              value={templateId}
              onChange={setTemplateId}
              placeholder={tpl.isLoading ? 'Carregando…' : 'Selecione'}
              options={templates.map((t) => ({ value: t.id, label: `${labelOf(CONTRACT_KIND_LABEL, t.kind)} — v${t.version}` }))}
            />
            {tpl.error ? <FieldDescription>{errorMessage(tpl.error)}</FieldDescription> : null}
            <FieldError>{touched ? errs.template : null}</FieldError>
          </Field>
          {needsAgreement ? (
            <Field invalid={touched && !!errs.agreement} required>
              <FieldLabel>Acordo de comodato</FieldLabel>
              <OptionSelect
                value={agreementId}
                onChange={setAgreementId}
                options={agreements
                  .filter((a) => a.status === 'ACTIVE')
                  .map((a) => ({ value: a.id, label: `Acordo de ${formatDateTime(a.started_at, est.timezone)}` }))}
              />
              <FieldError>{touched ? errs.agreement : null}</FieldError>
            </Field>
          ) : null}
          <Field invalid={touched && !!errs.signer} required>
            <FieldLabel>Nome de quem assinou</FieldLabel>
            <Input value={signer} onChange={(e) => setSigner(e.target.value)} autoComplete="off" />
            <FieldError>{touched ? errs.signer : null}</FieldError>
          </Field>
          <Field invalid={touched && !!errs.notes} required>
            <FieldLabel>Observações (local do documento físico)</FieldLabel>
            <Textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={3} maxLength={2000} />
            <FieldError>{touched ? errs.notes : null}</FieldError>
          </Field>
          {m.error ? (
            <Alert variant="danger">
              <AlertDescription className="text-text">{errorMessage(m.error)}</AlertDescription>
            </Alert>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)} disabled={m.isPending}>
              Cancelar
            </Button>
            <Button type="submit" loading={m.isPending}>
              Registrar contrato
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
