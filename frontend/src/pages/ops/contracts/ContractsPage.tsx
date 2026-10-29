/**
 * Contratos e termos (RD): modelos por tipo (parceria, comodato, termos do anunciante) e versões
 * (rascunho → publicada → substituída). Criar nova versão em rascunho; publicar no detalhe.
 * Backend: GET/POST /contracts/templates (organizations.manage global para criar/publicar).
 */
import * as React from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { FilePlus2, FileText } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Can } from '@/components/auth/guards'
import { Card, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Skeleton } from '@/components/ui/skeleton'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { EmptyState, ErrorState, NoPermission } from '@/components/patterns/states'
import { OptionSelect } from '@/components/ops/filter-select'
import { errorMessage, isApiError } from '@/lib/errors'
import { qk } from '@/lib/queryKeys'
import { formatDateTime } from '@/lib/format'
import { contractsApi } from '@/pages/ops/api/client'
import { CONTRACT_KIND_LABEL, CONTRACT_STATUS_LABEL, labelOf } from '@/pages/ops/api/labels'
import type { ContractKind, ContractTemplate } from '@/pages/ops/api/types'

const KINDS: ContractKind[] = ['PARTNERSHIP', 'COMODATO', 'ADVERTISER_TERMS']
const statusTone = (s: string) => (s === 'PUBLISHED' ? 'success' : s === 'DRAFT' ? 'warning' : 'neutral')

export default function ContractsPage() {
  const [open, setOpen] = React.useState(false)
  const q = useQuery({ queryKey: qk.sub('establishments', 'contracts', 'templates', 'all'), queryFn: () => contractsApi.templates() })
  if (isApiError(q.error) && q.error.isForbidden) return <NoPermission permission="organizations.manage" />
  const byKind = (k: ContractKind) => (q.data?.items ?? []).filter((t) => t.kind === k)
  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Contratos e termos"
        description="Cada aceite fica vinculado à versão exata do texto (com hash SHA-256)."
        actions={
          <Can permission="organizations.manage">
            <Button onClick={() => setOpen(true)}>
              <FilePlus2 aria-hidden="true" />
              Nova versão
            </Button>
          </Can>
        }
      />
      {q.isLoading ? (
        <Skeleton className="h-64 w-full rounded-xl" />
      ) : q.error ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} />
      ) : (
        <div className="grid grid-cols-1 gap-6 xl:grid-cols-3">
          {KINDS.map((k) => (
            <Card key={k}>
              <CardHeader>
                <CardTitle>{CONTRACT_KIND_LABEL[k]}</CardTitle>
              </CardHeader>
              {!byKind(k).length ? (
                <EmptyState icon={FileText} size="compact" headingLevel="h3" title="Nenhuma versão cadastrada." />
              ) : (
                <ul className="m-0 flex list-none flex-col divide-y divide-border p-0">
                  {byKind(k).map((t: ContractTemplate) => (
                    <li key={t.id} className="flex flex-col gap-1 py-3">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <Link to={`/contratos/${t.id}`} className="font-semibold text-link underline-offset-4 hover:underline">
                          Versão {t.version} — {t.title}
                        </Link>
                        <Badge tone={statusTone(t.status)}>{labelOf(CONTRACT_STATUS_LABEL, t.status)}</Badge>
                      </div>
                      <span className="text-xs text-text-muted">
                        {t.published_at ? `Publicada em ${formatDateTime(t.published_at)}` : `Criada em ${formatDateTime(t.created_at)}`}
                        {t.retired_at ? ` · substituída em ${formatDateTime(t.retired_at)}` : ''}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </Card>
          ))}
        </div>
      )}
      <NewTemplateDialog open={open} onOpenChange={setOpen} />
    </div>
  )
}

function NewTemplateDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [kind, setKind] = React.useState<string | null>('PARTNERSHIP')
  const [title, setTitle] = React.useState('')
  const [body, setBody] = React.useState('')
  const [touched, setTouched] = React.useState(false)
  React.useEffect(() => {
    if (open) {
      setKind('PARTNERSHIP')
      setTitle('')
      setBody('')
      setTouched(false)
    }
  }, [open])
  const errs = {
    title: title.trim().length < 5 ? 'Título com pelo menos 5 caracteres.' : null,
    body: body.trim().length < 50 ? 'Texto do contrato com pelo menos 50 caracteres.' : null,
  }
  const m = useMutation({
    mutationFn: () => contractsApi.createTemplate({ kind, title: title.trim(), body }),
    onSuccess: (t) => {
      void qc.invalidateQueries({ queryKey: ['establishments'] })
      toast.success(`Rascunho da versão ${t.version} criado.`)
      onOpenChange(false)
      navigate(`/contratos/${t.id}`)
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
            if (!errs.title && !errs.body) m.mutate()
          }}
        >
          <DialogHeader>
            <DialogTitle>Nova versão de contrato</DialogTitle>
            <DialogDescription>Fica como rascunho até ser publicada. Versões publicadas não podem ser editadas.</DialogDescription>
          </DialogHeader>
          <Field required>
            <FieldLabel>Tipo</FieldLabel>
            <OptionSelect value={kind} onChange={(v) => setKind(v ?? 'PARTNERSHIP')} options={KINDS.map((k) => ({ value: k, label: CONTRACT_KIND_LABEL[k] }))} />
          </Field>
          <Field invalid={touched && !!errs.title} required>
            <FieldLabel>Título</FieldLabel>
            <Input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={200} />
            <FieldError>{touched ? errs.title : null}</FieldError>
          </Field>
          <Field invalid={touched && !!errs.body} required>
            <FieldLabel>Texto do contrato</FieldLabel>
            <Textarea value={body} onChange={(e) => setBody(e.target.value)} rows={12} maxLength={200000} />
            <FieldDescription>Texto puro; quebras de linha são preservadas no PDF do aceite.</FieldDescription>
            <FieldError>{touched ? errs.body : null}</FieldError>
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
              Criar rascunho
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
