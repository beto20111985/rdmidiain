/**
 * Comprovantes de veiculação (P001 §104; ETAPA2 #807–#809; REQ-REL-004): emitir e baixar o
 * PDF assinado (ES256 da instância) com as EXIBIÇÕES VÁLIDAS comprovadas (proof-of-play) —
 * a programação planejada nunca entra. Telas por código e região (sem nome de local).
 */
import * as React from 'react'
import { Link } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Download, FileCheck2, ShieldCheck } from 'lucide-react'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState, ErrorState } from '@/components/patterns/states'
import { formatIsoDate, isIsoDate } from '@/components/finance/period'
import { useDownload } from '@/components/finance/use-download'
import { useIsMobile } from '@/hooks/useMediaQuery'
import { useCan } from '@/hooks/usePermissions'
import { http, type Paginated } from '@/lib/http'
import { qk } from '@/lib/queryKeys'
import { errorMessage } from '@/lib/errors'
import { formatDateTime, formatNumber } from '@/lib/format'
import { apiPath, reportsApi, type DeliveryProof } from './api'

interface CampaignLite {
  id: string
  name: string
  status: string
}

export function ProofsPanel({ orgId }: { orgId?: string | null }) {
  const qc = useQueryClient()
  const can = useCan()
  const isMobile = useIsMobile()
  const dl = useDownload()
  const [campaignId, setCampaignId] = React.useState<string | undefined>(undefined)
  const [start, setStart] = React.useState('')
  const [end, setEnd] = React.useState('')
  const [error, setError] = React.useState<string | null>(null)
  const [fieldError, setFieldError] = React.useState<string | null>(null)
  const [pending, setPending] = React.useState(false)
  const campaigns = useQuery({
    queryKey: qk.list('campaigns', { org_id: orgId ?? undefined, page_size: 100, picker: 'proofs' }),
    queryFn: () => http.get<Paginated<CampaignLite>>('/campaigns', { query: { org_id: orgId ?? undefined, page_size: 100 } }),
    enabled: can('campaigns.view'),
  })
  const proofs = useQuery({ queryKey: qk.sub('reports', 'proofs', orgId ?? 'all'), queryFn: () => reportsApi.proofs({}) })
  const nameOf = (id: string) => campaigns.data?.items.find((c) => c.id === id)?.name ?? `Campanha ${id.slice(0, 8)}`

  const issue = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!campaignId) {
      setFieldError('Escolha a campanha.')
      return
    }
    if ((start && !isIsoDate(start)) || (end && !isIsoDate(end)) || (start && end && end < start)) {
      setFieldError('Período inválido.')
      return
    }
    setFieldError(null)
    setError(null)
    setPending(true)
    try {
      const p = await reportsApi.createProof({ campaign_id: campaignId, start: start || undefined, end: end || undefined })
      toast.success(`Comprovante ${p.code} emitido: ${formatNumber(p.total_valid_plays)} exibições válidas.`)
      await qc.invalidateQueries({ queryKey: qk.sub('reports', 'proofs') })
      await dl.run(p.id, apiPath(p.pdf_url), { fallbackFilename: `comprovante-${p.code}.pdf` })
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setPending(false)
    }
  }

  const download = (p: DeliveryProof) => void dl.run(p.id, apiPath(p.pdf_url), { fallbackFilename: `comprovante-${p.code}.pdf` })
  const items = proofs.data?.items ?? []

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <div className="flex flex-col gap-1">
            <CardTitle>Emitir comprovante de veiculação</CardTitle>
            <CardDescription>
              Conta somente exibições válidas comprovadas pelos players (100% da duração). Agendamento não é prova de exibição.
            </CardDescription>
          </div>
          <FileCheck2 aria-hidden="true" className="size-5 text-text-muted" />
        </CardHeader>
        {error ? (
          <Alert variant="danger">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        ) : null}
        <form onSubmit={issue} noValidate className="flex flex-wrap items-end gap-3">
          <Field invalid={!!fieldError} className="w-full max-w-sm">
            <FieldLabel required>Campanha</FieldLabel>
            <Select
              value={campaignId}
              onValueChange={(v) => {
                setCampaignId(v)
                setFieldError(null)
              }}
            >
              <SelectTrigger>
                <SelectValue placeholder={campaigns.isLoading ? 'Carregando…' : 'Selecione a campanha'} />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  {(campaigns.data?.items ?? []).map((c) => (
                    <SelectItem key={c.id} value={c.id}>
                      {c.name}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
            <FieldError>{fieldError}</FieldError>
          </Field>
          <Field className="w-full max-w-[180px]">
            <FieldLabel>De</FieldLabel>
            <Input type="date" value={start} onChange={(e) => setStart(e.target.value)} />
          </Field>
          <Field className="w-full max-w-[180px]">
            <FieldLabel>Até</FieldLabel>
            <Input type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
          </Field>
          <Button type="submit" loading={pending} loadingText="Emitindo…">
            <ShieldCheck aria-hidden="true" />
            Emitir e baixar PDF
          </Button>
        </form>
        <Field>
          <FieldDescription>
            Sem datas = período da campanha até hoje. Assinatura eletrônica ES256 da plataforma (não é certificado ICP-Brasil).{' '}
            <Link to="/comprovantes/verificar" className="font-semibold text-link underline-offset-4 hover:underline">
              Verificar um comprovante
            </Link>
          </FieldDescription>
        </Field>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Comprovantes emitidos</CardTitle>
        </CardHeader>
        {proofs.isLoading ? (
          <Skeleton className="h-32 w-full" />
        ) : proofs.error ? (
          <ErrorState error={proofs.error} onRetry={() => void proofs.refetch()} size="compact" />
        ) : !items.length ? (
          <EmptyState size="compact" headingLevel="h3" icon={FileCheck2} title="Nenhum comprovante emitido ainda." />
        ) : isMobile ? (
          <ul className="m-0 flex list-none flex-col divide-y divide-border p-0" aria-label="Comprovantes emitidos">
            {items.map((p) => (
              <li key={p.id} className="flex flex-col gap-2 py-3 first:pt-0 last:pb-0">
                <span className="font-mono font-semibold">{p.code}</span>
                <span className="text-xs text-text-muted">
                  {nameOf(p.campaign_id)} · {formatIsoDate(p.period_start)} a {formatIsoDate(p.period_end)} · {formatNumber(p.total_valid_plays)} exibições válidas
                </span>
                <Button variant="secondary" className="self-start" loading={dl.isPending(p.id)} loadingText="Baixando…" onClick={() => download(p)} aria-label={`Baixar comprovante ${p.code}`}>
                  <Download aria-hidden="true" />
                  PDF
                </Button>
              </li>
            ))}
          </ul>
        ) : (
          <Table>
            <caption className="sr-only">Comprovantes emitidos</caption>
            <TableHeader>
              <TableRow className="h-11 hover:bg-transparent">
                <TableHead scope="col">Código</TableHead>
                <TableHead scope="col">Campanha</TableHead>
                <TableHead scope="col">Período</TableHead>
                <TableHead scope="col" numeric>
                  Exibições válidas
                </TableHead>
                <TableHead scope="col">Emitido em</TableHead>
                <TableHead scope="col" className="text-right">
                  PDF
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((p) => (
                <TableRow key={p.id}>
                  <TableCell className="font-mono">{p.code}</TableCell>
                  <TableCell>{nameOf(p.campaign_id)}</TableCell>
                  <TableCell className="tabular-nums">
                    {formatIsoDate(p.period_start)} a {formatIsoDate(p.period_end)}
                  </TableCell>
                  <TableCell numeric>{formatNumber(p.total_valid_plays)}</TableCell>
                  <TableCell className="tabular-nums">{formatDateTime(p.created_at)}</TableCell>
                  <TableCell className="text-right">
                    <Button variant="secondary" loading={dl.isPending(p.id)} loadingText="Baixando…" onClick={() => download(p)} aria-label={`Baixar comprovante ${p.code}`}>
                      <Download aria-hidden="true" />
                      PDF
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Card>
    </div>
  )
}
