/**
 * Cadastros de parceiros (onboarding): fila de aprovação da RD, convites do comercial e cadastros
 * guiados pela equipe. Backend: GET/POST /onboarding, POST /onboarding/invites (W-ORG).
 * Auto-cadastro público: /cadastro-parceiro (link enviado por e-mail com token).
 */
import * as React from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { ClipboardList, MailPlus, Plus } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Can } from '@/components/auth/guards'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { DataTable, type DataTableColumn } from '@/components/patterns/data-table'
import { NoPermission } from '@/components/patterns/states'
import { FilterSelect } from '@/components/ops/filter-select'
import { RealtimeStale } from '@/components/ops/realtime-stale'
import { useTableState } from '@/hooks/useTableState'
import { errorMessage, isApiError } from '@/lib/errors'
import { qk } from '@/lib/queryKeys'
import { formatDateTime } from '@/lib/format'
import { onboardingApi } from '@/pages/ops/api/client'
import { ONBOARDING_ORIGIN_LABEL, ONBOARDING_STATUS_LABEL, labelOf } from '@/pages/ops/api/labels'
import type { OnboardingApplication } from '@/pages/ops/api/types'
import { onboardingStatusTone } from './stepRules'

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/

export default function OnboardingQueuePage() {
  const table = useTableState({ defaultPageSize: 25, filterKeys: ['status'] })
  const status = table.state.filters.status
  const params = { page: table.state.page, page_size: table.state.pageSize, status }
  const q = useQuery({
    queryKey: qk.sub('establishments', 'onboarding', 'list', params),
    queryFn: () => onboardingApi.list(params),
    placeholderData: keepPreviousData,
  })
  const [dialog, setDialog] = React.useState<'invite' | 'create' | null>(null)

  if (isApiError(q.error) && q.error.isForbidden) return <NoPermission permission="establishments.manage" />

  const columns: DataTableColumn<OnboardingApplication>[] = [
    {
      id: 'name',
      header: 'Estabelecimento',
      mobile: 'primary',
      cell: (a) => {
        const est = a.data.establishment as { name?: string } | undefined
        return (
          <Link to={`/estabelecimentos/cadastros/${a.id}`} className="font-semibold text-link underline-offset-4 hover:underline">
            {est?.name ?? 'Cadastro sem nome ainda'}
          </Link>
        )
      },
    },
    { id: 'email', header: 'E-mail', mobile: 'secondary', cell: (a) => a.email },
    {
      id: 'city',
      header: 'Cidade/UF',
      cell: (a) => {
        const ad = a.data.address as { city?: string; state?: string } | undefined
        return ad?.city ? `${ad.city}/${ad.state ?? ''}` : '—'
      },
    },
    { id: 'origin', header: 'Origem', cell: (a) => labelOf(ONBOARDING_ORIGIN_LABEL, a.origin) },
    {
      id: 'status',
      header: 'Status',
      mobile: 'badge',
      cell: (a) => <Badge tone={onboardingStatusTone(a.status)}>{labelOf(ONBOARDING_STATUS_LABEL, a.status)}</Badge>,
    },
    { id: 'submitted', header: 'Enviado em', cell: (a) => formatDateTime(a.submitted_at ?? a.created_at) },
  ]

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Cadastros de parceiros"
        description="Aprovação de auto-cadastros, convites do comercial e cadastros guiados pela equipe."
        breadcrumbs={[{ label: 'Estabelecimentos', to: '/estabelecimentos' }, { label: 'Cadastros' }]}
        actions={
          <div className="flex flex-wrap gap-2">
            <Can permission="organizations.manage">
              <Button variant="secondary" onClick={() => setDialog('invite')}>
                <MailPlus aria-hidden="true" />
                Convidar por e-mail
              </Button>
            </Can>
            <Can permission="establishments.manage">
              <Button onClick={() => setDialog('create')}>
                <Plus aria-hidden="true" />
                Cadastro guiado
              </Button>
            </Can>
          </div>
        }
      />
      <RealtimeStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />
      <Card>
        <DataTable
          caption="Cadastros de parceiros"
          columns={columns}
          data={q.data}
          getRowId={(a) => a.id}
          controller={table}
          isLoading={q.isLoading}
          isFetching={q.isFetching}
          error={q.error}
          onRetry={() => void q.refetch()}
          filtersActive={!!status}
          empty={{
            icon: ClipboardList,
            title: 'Nenhum cadastro de parceiro.',
            description: 'Convide o responsável por e-mail ou inicie um cadastro guiado.',
          }}
          toolbar={
            <FilterSelect
              label="Status"
              value={status}
              onChange={(v) => table.setFilter('status', v)}
              options={Object.entries(ONBOARDING_STATUS_LABEL).map(([value, label]) => ({ value, label }))}
              className="sm:w-56"
            />
          }
        />
      </Card>
      <EmailDialog mode={dialog} onClose={() => setDialog(null)} />
    </div>
  )
}

function EmailDialog({ mode, onClose }: { mode: 'invite' | 'create' | null; onClose: () => void }) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [email, setEmail] = React.useState('')
  const [touched, setTouched] = React.useState(false)
  React.useEffect(() => {
    if (mode) {
      setEmail('')
      setTouched(false)
    }
  }, [mode])
  const valid = EMAIL_RE.test(email.trim())
  const m = useMutation({
    mutationFn: () => (mode === 'invite' ? onboardingApi.invite(email.trim()) : onboardingApi.create({ email: email.trim() })),
    onSuccess: (a) => {
      void qc.invalidateQueries({ queryKey: ['establishments'] })
      if (mode === 'invite') {
        toast.success('Convite enviado. O link vale por 7 dias.')
        onClose()
      } else {
        onClose()
        navigate(`/estabelecimentos/cadastros/${a.id}`)
      }
    },
  })
  return (
    <Dialog open={!!mode} onOpenChange={(o) => !o && !m.isPending && onClose()}>
      <DialogContent>
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
            <DialogTitle>{mode === 'invite' ? 'Convidar estabelecimento' : 'Iniciar cadastro guiado'}</DialogTitle>
            <DialogDescription>
              {mode === 'invite'
                ? 'O responsável recebe um link (válido por 7 dias) para preencher o cadastro.'
                : 'Você preenche as etapas com o responsável; o e-mail identifica o cadastro.'}
            </DialogDescription>
          </DialogHeader>
          <Field invalid={touched && !valid} required>
            <FieldLabel>E-mail do responsável</FieldLabel>
            <Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="off" />
            <FieldDescription>Usado para o convite de acesso ao painel após a aprovação.</FieldDescription>
            <FieldError>{touched && !valid ? 'Informe um e-mail válido.' : null}</FieldError>
          </Field>
          {m.error ? (
            <Alert variant="danger">
              <AlertDescription className="text-text">{errorMessage(m.error)}</AlertDescription>
            </Alert>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={onClose} disabled={m.isPending}>
              Cancelar
            </Button>
            <Button type="submit" loading={m.isPending}>
              {mode === 'invite' ? 'Enviar convite' : 'Começar'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
