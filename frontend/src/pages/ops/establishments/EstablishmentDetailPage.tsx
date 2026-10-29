/**
 * Detalhe do estabelecimento (RD): dados fiscais, endereço/coordenadas, fuso (sugestão e correção),
 * categorias/segmentos/produtos, classe/peso, audiência ESTIMADA com método, plano, telas,
 * contratos/comodato e histórico. Inativar/reativar com motivo (e nome digitado para inativar).
 */
import * as React from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { KeyRound, Pencil, Power, PowerOff } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Can } from '@/components/auth/guards'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Card, CardHeader, CardTitle } from '@/components/ui/card'
import { ErrorState, NoPermission, PageSkeleton } from '@/components/patterns/states'
import { RealtimeStale } from '@/components/ops/realtime-stale'
import { ReasonDialog } from '@/components/ops/reason-dialog'
import { AuditHistory } from '@/components/ops/audit-history'
import { TechPinDialog } from '@/components/ops/tech-pin-dialog'
import { isApiError } from '@/lib/errors'
import { qk } from '@/lib/queryKeys'
import { formatDateTime } from '@/lib/format'
import { establishmentsApi } from '@/pages/ops/api/client'
import { ONBOARDING_STATUS_EST_LABEL, labelOf } from '@/pages/ops/api/labels'
import { EstablishmentOverview } from './tabs/EstablishmentOverview'
import { EstablishmentTaxonomy } from './tabs/EstablishmentTaxonomy'
import { EstablishmentAudience } from './tabs/EstablishmentAudience'
import { EstablishmentScreens } from './tabs/EstablishmentScreens'
import { EstablishmentContracts } from './tabs/EstablishmentContracts'

const TABS = ['visao', 'categorias', 'audiencia', 'telas', 'contratos', 'historico'] as const
type TabId = (typeof TABS)[number]

export default function EstablishmentDetailPage() {
  const { id = '' } = useParams()
  const [sp, setSp] = useSearchParams()
  const tab: TabId = (TABS as readonly string[]).includes(sp.get('aba') ?? '') ? (sp.get('aba') as TabId) : 'visao'
  const qc = useQueryClient()
  const [dialog, setDialog] = React.useState<'deactivate' | 'reactivate' | 'pin' | null>(null)

  const q = useQuery({ queryKey: qk.detail('establishments', id), queryFn: () => establishmentsApi.get(id) })
  const est = q.data

  const toggle = useMutation({
    mutationFn: ({ active, reason }: { active: boolean; reason: string }) =>
      active ? establishmentsApi.reactivate(id, reason) : establishmentsApi.deactivate(id, reason),
    onSuccess: (_d, v) => {
      void qc.invalidateQueries({ queryKey: ['establishments'] })
      void qc.invalidateQueries({ queryKey: ['screens'] })
      toast.success(v.active ? 'Estabelecimento reativado.' : 'Estabelecimento inativado.')
    },
  })

  const crumbs = React.useMemo(
    () => [{ label: 'Estabelecimentos', to: '/estabelecimentos' }, { label: est?.name ?? 'Estabelecimento' }],
    [est?.name],
  )

  if (q.isLoading) return <PageSkeleton />
  if (isApiError(q.error) && q.error.isForbidden) return <NoPermission permission="establishments.view" />
  if (q.error || !est)
    return (
      <ErrorState
        error={q.error}
        title={isApiError(q.error) && q.error.isNotFound ? 'Estabelecimento não encontrado ou fora do seu acesso' : undefined}
        onRetry={() => void q.refetch()}
        retrying={q.isFetching}
      />
    )

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title={est.name}
        description={`${est.city}/${est.state} · fuso ${est.timezone}`}
        breadcrumbs={crumbs}
        actions={
          <div className="flex flex-wrap gap-2">
            <Can permission="screens.command">
              {est.is_active ? (
                <Button variant="secondary" onClick={() => setDialog('pin')}>
                  <KeyRound aria-hidden="true" />
                  PIN técnico
                </Button>
              ) : null}
            </Can>
            <Can permission="establishments.manage">
              {est.is_active ? (
                <Button variant="destructive" onClick={() => setDialog('deactivate')}>
                  <PowerOff aria-hidden="true" />
                  Inativar
                </Button>
              ) : (
                <Button variant="secondary" onClick={() => setDialog('reactivate')}>
                  <Power aria-hidden="true" />
                  Reativar
                </Button>
              )}
              <Button asChild>
                <Link to={`/estabelecimentos/${est.id}/editar`}>
                  <Pencil aria-hidden="true" />
                  Editar
                </Link>
              </Button>
            </Can>
          </div>
        }
      />
      <RealtimeStale updatedAt={q.dataUpdatedAt} onRefresh={() => void q.refetch()} refreshing={q.isFetching} />

      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={est.is_active ? 'success' : 'neutral'}>{est.is_active ? 'Ativo' : 'Inativo'}</Badge>
        <Badge tone={est.onboarding_status === 'ACTIVE' ? 'success' : 'warning'}>
          {labelOf(ONBOARDING_STATUS_EST_LABEL, est.onboarding_status)}
        </Badge>
        {est.venue_class === 'PREMIUM' ? <Badge tone="info">Premium</Badge> : null}
      </div>

      {!est.is_active ? (
        <Alert variant="warning">
          <AlertTitle>Estabelecimento inativo</AlertTitle>
          <AlertDescription>
            Inativado em {formatDateTime(est.deactivated_at, est.timezone)}. Motivo: {est.deactivation_reason ?? '—'}
          </AlertDescription>
        </Alert>
      ) : est.onboarding_status === 'AWAITING_CONTRACT' ? (
        <Alert variant="info">
          <AlertTitle>Aguardando o aceite do contrato de parceria</AlertTitle>
          <AlertDescription>
            As telas deste estabelecimento só ficam disponíveis para venda depois do aceite do contrato pelo responsável (ou do
            registro do contrato em papel na aba Contratos).
          </AlertDescription>
        </Alert>
      ) : null}

      <Tabs value={tab} onValueChange={(v) => setSp((p) => { const n = new URLSearchParams(p); n.set('aba', v); return n }, { replace: true })}>
        <TabsList aria-label="Seções do estabelecimento">
          <TabsTrigger value="visao">Dados</TabsTrigger>
          <TabsTrigger value="categorias">Categorias e produtos</TabsTrigger>
          <TabsTrigger value="audiencia">Audiência estimada</TabsTrigger>
          <TabsTrigger value="telas">Telas{typeof est.screens_count === 'number' ? ` (${est.screens_count})` : ''}</TabsTrigger>
          <TabsTrigger value="contratos">Contratos e comodato</TabsTrigger>
          <TabsTrigger value="historico">Histórico</TabsTrigger>
        </TabsList>
        <TabsContent value="visao">
          <EstablishmentOverview est={est} />
        </TabsContent>
        <TabsContent value="categorias">
          <EstablishmentTaxonomy est={est} />
        </TabsContent>
        <TabsContent value="audiencia">
          <EstablishmentAudience est={est} />
        </TabsContent>
        <TabsContent value="telas">
          <EstablishmentScreens est={est} />
        </TabsContent>
        <TabsContent value="contratos">
          <EstablishmentContracts est={est} />
        </TabsContent>
        <TabsContent value="historico">
          <Card>
            <CardHeader>
              <CardTitle>Histórico de alterações</CardTitle>
            </CardHeader>
            <AuditHistory entityType="establishment" entityId={est.id} timeZone={est.timezone} />
          </Card>
        </TabsContent>
      </Tabs>

      <ReasonDialog
        open={dialog === 'deactivate'}
        onOpenChange={(o) => setDialog(o ? 'deactivate' : null)}
        title={`Inativar ${est.name}?`}
        description="A inativação é auditada e pode ser desfeita com reativação."
        consequences={[
          'Todas as telas deste estabelecimento ficam inativas e saem do inventário vendável.',
          'Campanhas em andamento deixam de ser exibidas nessas telas na próxima sincronização.',
          'O histórico, contratos e equipamentos são preservados.',
        ]}
        confirmLabel="Inativar estabelecimento"
        destructive
        requireText={est.name}
        onConfirm={(reason) => toggle.mutateAsync({ active: false, reason })}
      />
      <ReasonDialog
        open={dialog === 'reactivate'}
        onOpenChange={(o) => setDialog(o ? 'reactivate' : null)}
        title={`Reativar ${est.name}?`}
        consequences={['As telas voltam ao status anterior e ao inventário (se o contrato estiver aceito).']}
        confirmLabel="Reativar estabelecimento"
        onConfirm={(reason) => toggle.mutateAsync({ active: true, reason })}
      />
      <TechPinDialog
        open={dialog === 'pin'}
        onOpenChange={(o) => setDialog(o ? 'pin' : null)}
        targetLabel={`todas as telas pareadas de ${est.name}`}
        onSubmit={async (pin, reason) => {
          const res = await establishmentsApi.setTechPin(est.id, { pin, reason })
          toast.success(`PIN técnico enviado para ${res.total} tela(s) pareada(s).`)
        }}
      />
    </div>
  )
}
