/**
 * Minha conta → Privacidade e dados (LGPD do titular): consentimentos com versão
 * (GET/POST /me/consents), exportação dos meus dados (GET /me/data-export) e pedido de
 * exclusão (POST /me/deletion-request). Bloqueado durante impersonation (o servidor recusa).
 */
import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Download, FileCheck2, Trash2 } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ErrorState, PageSkeleton } from '@/components/patterns/states'
import { useAuthStore } from '@/stores/authStore'
import { http } from '@/lib/http'
import { errorMessage } from '@/lib/errors'
import { formatDateTime } from '@/lib/format'
import { saveBlob } from '@/components/admin/binary'
import { CONSENT_DOCUMENTS, labelOf } from '@/components/admin/labels'
import { ReasonDialog } from '@/components/admin/ui'

interface ConsentEntry {
  document: string
  version: string
  action: 'accepted' | 'revoked'
  at: string
}

interface ConsentsResponse {
  current_versions: Record<string, string>
  consents: ConsentEntry[]
  history: ConsentEntry[]
}

const DOCS = ['terms', 'privacy', 'cookies', 'marketing'] as const

export default function MyPrivacyPage() {
  const me = useAuthStore((s) => s.me)
  const qc = useQueryClient()
  const impersonating = !!me?.impersonation?.active
  const q = useQuery({ queryKey: ['me', 'consents'], queryFn: () => http.get<ConsentsResponse>('/me/consents') })
  const [exporting, setExporting] = React.useState(false)
  const [deleting, setDeleting] = React.useState(false)
  const [deletion, setDeletion] = React.useState<{ status: string; requested_at: string } | null>(null)

  const consent = useMutation({
    mutationFn: (v: { document: string; action: 'accepted' | 'revoked'; version?: string }) => http.post('/me/consents', v),
    onSuccess: (_d, v) => {
      toast.success(v.action === 'accepted' ? 'Consentimento registrado.' : 'Consentimento revogado.')
      void qc.invalidateQueries({ queryKey: ['me', 'consents'] })
    },
    onError: (e) => toast.error(errorMessage(e)),
  })

  const exportData = async () => {
    setExporting(true)
    try {
      const data = await http.get<unknown>('/me/data-export')
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
      saveBlob(blob, `meus-dados-rd-midia-indoor-${new Date().toISOString().slice(0, 10)}.json`)
      toast.success('Arquivo com os seus dados gerado.')
    } catch (e) {
      toast.error(errorMessage(e))
    } finally {
      setExporting(false)
    }
  }

  if (q.isLoading) return <PageSkeleton />
  if (q.error || !q.data) return <ErrorState error={q.error} onRetry={() => void q.refetch()} retrying={q.isFetching} />
  const data = q.data
  const currentOf = (doc: string) => data.consents.find((c) => c.document === doc) ?? null

  return (
    <>
      <PageHeader title="Privacidade e dados" breadcrumbs={[{ label: 'Minha conta', to: '/minha-conta' }, { label: 'Privacidade e dados' }]} />
      <div className="flex flex-col gap-4 xl:gap-6">
        {impersonating ? (
          <Alert variant="warning">
            <AlertDescription>Durante o acesso como outro usuário, consentimentos, exportação e exclusão ficam bloqueados.</AlertDescription>
          </Alert>
        ) : null}
        <Card>
          <CardHeader>
            <div className="flex flex-col gap-1">
              <CardTitle>Consentimentos</CardTitle>
              <CardDescription>Cada aceite ou revogação é registrado com versão, data, IP e navegador.</CardDescription>
            </div>
          </CardHeader>
          <ul className="m-0 flex list-none flex-col divide-y divide-border p-0">
            {DOCS.map((doc) => {
              const cur = currentOf(doc)
              const version = data.current_versions[doc]
              const accepted = cur?.action === 'accepted'
              const outdated = accepted && version && cur?.version !== version
              const required = doc === 'terms' || doc === 'privacy'
              return (
                <li key={doc} className="flex flex-col gap-3 py-4 first:pt-0 last:pb-0 md:flex-row md:items-center md:justify-between">
                  <div className="flex flex-col gap-1">
                    <span className="flex flex-wrap items-center gap-2 font-semibold text-text">
                      {labelOf(CONSENT_DOCUMENTS, doc)}
                      {accepted ? <Badge tone={outdated ? 'warning' : 'success'}>{outdated ? 'Nova versão disponível' : 'Aceito'}</Badge> : <Badge tone="neutral">Não aceito</Badge>}
                    </span>
                    <span className="text-xs text-text-muted">
                      {cur ? `${cur.action === 'accepted' ? 'Aceito' : 'Revogado'} na versão ${cur.version} em ${formatDateTime(cur.at)}` : 'Sem registro.'}
                      {version ? ` · Versão vigente: ${version}` : ''}
                    </span>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {!accepted || outdated ? (
                      <Button
                        variant="secondary"
                        disabled={impersonating}
                        loading={consent.isPending && consent.variables?.document === doc && consent.variables.action === 'accepted'}
                        onClick={() => consent.mutate({ document: doc, action: 'accepted', version })}
                      >
                        <FileCheck2 aria-hidden="true" />
                        {version ? `Aceitar versão ${version}` : 'Aceitar'}
                      </Button>
                    ) : null}
                    {accepted && !required ? (
                      <Button
                        variant="tertiary"
                        disabled={impersonating}
                        loading={consent.isPending && consent.variables?.document === doc && consent.variables.action === 'revoked'}
                        onClick={() => consent.mutate({ document: doc, action: 'revoked' })}
                      >
                        Revogar
                      </Button>
                    ) : null}
                  </div>
                </li>
              )
            })}
          </ul>
          {data.history.length ? (
            <details className="text-sm">
              <summary className="min-h-11 cursor-pointer py-2 font-semibold text-link">Histórico completo ({data.history.length})</summary>
              <div className="overflow-x-auto">
                <Table>
                  <caption className="sr-only">Histórico de consentimentos</caption>
                  <TableHeader>
                    <TableRow>
                      <TableHead scope="col">Documento</TableHead>
                      <TableHead scope="col">Versão</TableHead>
                      <TableHead scope="col">Ação</TableHead>
                      <TableHead scope="col">Data</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {data.history.map((h, i) => (
                      <TableRow key={i}>
                        <TableCell>{labelOf(CONSENT_DOCUMENTS, h.document)}</TableCell>
                        <TableCell>{h.version}</TableCell>
                        <TableCell>{h.action === 'accepted' ? 'Aceito' : 'Revogado'}</TableCell>
                        <TableCell className="tabular-nums">{formatDateTime(h.at)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            </details>
          ) : null}
        </Card>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:gap-6">
          <Card>
            <CardHeader>
              <div className="flex flex-col gap-1">
                <CardTitle>Exportar meus dados</CardTitle>
                <CardDescription>Perfil, perfis de acesso, sessões, consentimentos e atividade recente, em JSON (LGPD art. 18).</CardDescription>
              </div>
            </CardHeader>
            <Button variant="secondary" onClick={() => void exportData()} loading={exporting} disabled={impersonating} className="w-fit">
              <Download aria-hidden="true" />
              Baixar meus dados
            </Button>
          </Card>
          <Card>
            <CardHeader>
              <div className="flex flex-col gap-1">
                <CardTitle>Excluir minha conta</CardTitle>
                <CardDescription>
                  A equipe RD analisa o pedido. Na exclusão, seus dados pessoais são anonimizados; registros financeiros, a trilha de auditoria e os
                  consentimentos são preservados por obrigação legal, sem identificar você.
                </CardDescription>
              </div>
            </CardHeader>
            {deletion ? (
              <Alert variant="info">
                <div className="flex min-w-0 flex-1 flex-col gap-1">
                  <AlertTitle>Pedido de exclusão recebido</AlertTitle>
                  <AlertDescription>
                    Situação: {deletion.status === 'PENDING' ? 'em análise' : deletion.status}. Registrado em {formatDateTime(deletion.requested_at)}.
                  </AlertDescription>
                </div>
              </Alert>
            ) : me?.user.is_admin_master ? (
              <p className="text-sm text-text-muted">Contas de Admin Master não podem ser excluídas por esta rota.</p>
            ) : (
              <Button variant="destructive" onClick={() => setDeleting(true)} disabled={impersonating} className="w-fit">
                <Trash2 aria-hidden="true" />
                Pedir exclusão da conta
              </Button>
            )}
          </Card>
        </div>
      </div>
      <ReasonDialog
        open={deleting}
        onOpenChange={setDeleting}
        title="Pedir a exclusão da sua conta?"
        consequences={[
          'Após o processamento você perde o acesso ao painel.',
          'Nome, e-mail e telefone são anonimizados; registros exigidos por lei são mantidos sem identificar você.',
        ]}
        reasonLabel="Motivo (opcional)"
        minLength={0}
        maxLength={2000}
        requireText={me?.user.email ?? undefined}
        confirmLabel="Enviar pedido de exclusão"
        destructive
        onConfirm={async (reason) => {
          const r = await http.post<{ id: string; status: string; requested_at: string }>('/me/deletion-request', { reason: reason || null })
          setDeletion(r)
          toast.success('Pedido de exclusão enviado.')
        }}
      />
    </>
  )
}
