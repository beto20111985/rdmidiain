/**
 * Versão de contrato: texto (renderizado como TEXTO, nunca HTML), hash, status; editar rascunho e
 * publicar (a versão publicada anterior do mesmo tipo é substituída). PATCH/POST publish (RD global).
 */
import * as React from 'react'
import { useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Send } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Field, FieldError, FieldLabel } from '@/components/ui/field'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { ConfirmDialog } from '@/components/patterns/confirm-dialog'
import { ErrorState, NoPermission, PageSkeleton } from '@/components/patterns/states'
import { DetailList } from '@/components/ops/detail-list'
import { useCan } from '@/hooks/usePermissions'
import { errorMessage, isApiError } from '@/lib/errors'
import { qk } from '@/lib/queryKeys'
import { formatDateTime } from '@/lib/format'
import { contractsApi } from '@/pages/ops/api/client'
import { CONTRACT_KIND_LABEL, CONTRACT_STATUS_LABEL, labelOf } from '@/pages/ops/api/labels'

export default function ContractTemplatePage() {
  const { id = '' } = useParams()
  const can = useCan()
  const qc = useQueryClient()
  const q = useQuery({ queryKey: qk.sub('establishments', 'contracts', 'template', id), queryFn: () => contractsApi.template(id) })
  const t = q.data
  const [title, setTitle] = React.useState('')
  const [body, setBody] = React.useState('')
  const [publishOpen, setPublishOpen] = React.useState(false)
  React.useEffect(() => {
    if (t) {
      setTitle(t.title)
      setBody(t.body ?? '')
    }
  }, [t])
  const save = useMutation({
    mutationFn: () => contractsApi.updateTemplate(id, { title: title.trim(), body }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['establishments'] })
      toast.success('Rascunho salvo.')
    },
  })
  const crumbs = React.useMemo(() => [{ label: 'Contratos', to: '/contratos' }, { label: t ? `Versão ${t.version}` : 'Versão' }], [t])

  if (q.isLoading) return <PageSkeleton />
  if (isApiError(q.error) && q.error.isForbidden) return <NoPermission permission="organizations.manage" />
  if (q.error || !t) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />

  const editable = t.status === 'DRAFT' && can('organizations.manage')
  const dirty = title.trim() !== t.title || body !== (t.body ?? '')
  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title={`${labelOf(CONTRACT_KIND_LABEL, t.kind)} — versão ${t.version}`}
        breadcrumbs={crumbs}
        actions={
          editable ? (
            <Button onClick={() => setPublishOpen(true)} disabled={dirty}>
              <Send aria-hidden="true" />
              Publicar versão
            </Button>
          ) : undefined
        }
      />
      <Card>
        <CardHeader>
          <CardTitle>Versão</CardTitle>
          <Badge tone={t.status === 'PUBLISHED' ? 'success' : t.status === 'DRAFT' ? 'warning' : 'neutral'}>
            {labelOf(CONTRACT_STATUS_LABEL, t.status)}
          </Badge>
        </CardHeader>
        <DetailList
          columns={3}
          items={[
            { label: 'Tipo', value: labelOf(CONTRACT_KIND_LABEL, t.kind) },
            { label: 'Versão', value: t.version },
            { label: 'Hash do texto (SHA-256)', value: t.body_sha256 ? <code className="break-all text-xs">{t.body_sha256}</code> : null },
            { label: 'Criada em', value: formatDateTime(t.created_at) },
            { label: 'Publicada em', value: formatDateTime(t.published_at) },
            { label: 'Substituída em', value: formatDateTime(t.retired_at) },
          ]}
        />
      </Card>
      {editable ? (
        <Card>
          <CardHeader>
            <CardTitle>Editar rascunho</CardTitle>
          </CardHeader>
          <Field invalid={title.trim().length < 5} required>
            <FieldLabel>Título</FieldLabel>
            <Input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={200} />
            <FieldError>{title.trim().length < 5 ? 'Mínimo de 5 caracteres.' : null}</FieldError>
          </Field>
          <Field invalid={body.trim().length < 50} required>
            <FieldLabel>Texto</FieldLabel>
            <Textarea value={body} onChange={(e) => setBody(e.target.value)} rows={16} maxLength={200000} />
            <FieldError>{body.trim().length < 50 ? 'Mínimo de 50 caracteres.' : null}</FieldError>
          </Field>
          {save.error ? (
            <Alert variant="danger">
              <AlertDescription className="text-text">{errorMessage(save.error)}</AlertDescription>
            </Alert>
          ) : null}
          <Button className="w-fit" onClick={() => save.mutate()} loading={save.isPending} disabled={!dirty || title.trim().length < 5 || body.trim().length < 50}>
            Salvar rascunho
          </Button>
          {dirty ? <p className="m-0 text-xs text-text-muted">Salve o rascunho antes de publicar.</p> : null}
        </Card>
      ) : (
        <Card>
          <CardHeader>
            <CardTitle>{t.title}</CardTitle>
          </CardHeader>
          <div className="max-h-[60vh] overflow-y-auto whitespace-pre-wrap rounded-lg border border-border bg-field p-4 text-sm leading-relaxed text-text" tabIndex={0} aria-label="Texto do contrato">
            {t.body}
          </div>
        </Card>
      )}
      <ConfirmDialog
        open={publishOpen}
        onOpenChange={setPublishOpen}
        title={`Publicar a versão ${t.version}?`}
        description="Depois de publicada, o texto não pode mais ser alterado."
        consequences={[
          'A versão publicada anterior deste tipo passa a “Substituída”.',
          'Novos aceites usarão esta versão; aceites antigos continuam vinculados à versão que foi aceita.',
        ]}
        confirmLabel="Publicar"
        requireText={`v${t.version}`}
        confirmHint="Digitar a versão evita publicar o texto errado."
        onConfirm={async () => {
          await contractsApi.publish(id)
          void qc.invalidateQueries({ queryKey: ['establishments'] })
          toast.success('Versão publicada.')
        }}
      />
    </div>
  )
}
