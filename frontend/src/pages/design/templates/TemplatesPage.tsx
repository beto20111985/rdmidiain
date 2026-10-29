/** Templates (RD, `templates.manage`) — GET /templates com filtros de situação e orientação. */
import * as React from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { LayoutTemplate, Plus } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { EmptyState, ErrorState, PageSkeleton } from '@/components/patterns/states'
import { http } from '@/lib/http'
import { formatDateTime } from '@/lib/format'
import type { DesignTemplateRow } from '../types'

const STATUS_LABEL: Record<string, { label: string; tone: 'neutral' | 'success' | 'info' }> = {
  RASCUNHO: { label: 'Rascunho', tone: 'neutral' },
  ATIVO: { label: 'Ativo', tone: 'success' },
  ARQUIVADO: { label: 'Arquivado', tone: 'neutral' },
}

export default function TemplatesPage() {
  const [status, setStatus] = React.useState('all')
  const [orientation, setOrientation] = React.useState('all')
  const params = { status: status === 'all' ? undefined : status, orientation: orientation === 'all' ? undefined : orientation }
  const q = useQuery({ queryKey: ['design', 'templates', params], queryFn: () => http.get<{ items: DesignTemplateRow[] }>('/templates', { query: params }).then((r) => r.items) })
  const action = (
    <Button asChild>
      <Link to="/design/templates/novo">
        <Plus aria-hidden="true" />
        Novo template
      </Link>
    </Button>
  )
  return (
    <>
      <PageHeader title="Templates" description="Modelos de arte por formulário (grade 24×24), renderizados no servidor em 1920×1080 ou 1080×1920." actions={action} />
      <Card>
        <div className="flex flex-wrap gap-3">
          <ToggleGroup type="single" aria-label="Situação" value={status} onValueChange={(v) => v && setStatus(v)}>
            <ToggleGroupItem value="all">Todos</ToggleGroupItem>
            <ToggleGroupItem value="ATIVO">Ativos</ToggleGroupItem>
            <ToggleGroupItem value="RASCUNHO">Rascunhos</ToggleGroupItem>
            <ToggleGroupItem value="ARQUIVADO">Arquivados</ToggleGroupItem>
          </ToggleGroup>
          <ToggleGroup type="single" aria-label="Orientação" value={orientation} onValueChange={(v) => v && setOrientation(v)}>
            <ToggleGroupItem value="all">Todas</ToggleGroupItem>
            <ToggleGroupItem value="HORIZONTAL">Horizontal</ToggleGroupItem>
            <ToggleGroupItem value="VERTICAL">Vertical</ToggleGroupItem>
          </ToggleGroup>
        </div>
        {q.isLoading ? (
          <PageSkeleton label="Carregando templates…" />
        ) : q.error ? (
          <ErrorState error={q.error} onRetry={() => void q.refetch()} size="compact" />
        ) : !q.data?.length ? (
          <EmptyState size="compact" headingLevel="h3" icon={LayoutTemplate} title="Nenhum template ainda." description="Crie modelos reutilizáveis para gerar artes rapidamente." action={action} />
        ) : (
          <div className="overflow-x-auto">
            <Table>
              <caption className="sr-only">Templates</caption>
              <TableHeader>
                <TableRow>
                  <TableHead scope="col">Nome</TableHead>
                  <TableHead scope="col">Formato</TableHead>
                  <TableHead scope="col">Situação</TableHead>
                  <TableHead scope="col">Versão</TableHead>
                  <TableHead scope="col">Atualizado</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {q.data.map((t) => (
                  <TableRow key={t.id}>
                    <TableCell>
                      <Link to={`/design/templates/${t.id}`} className="font-semibold text-link underline-offset-4 hover:underline">
                        {t.name}
                      </Link>
                      {t.description ? <span className="block max-w-[360px] truncate text-xs text-text-muted">{t.description}</span> : null}
                    </TableCell>
                    <TableCell className="tabular-nums">
                      {t.orientation === 'VERTICAL' ? 'Vertical' : 'Horizontal'} {t.width}×{t.height}
                    </TableCell>
                    <TableCell>
                      <Badge tone={STATUS_LABEL[t.status]?.tone ?? 'neutral'}>{STATUS_LABEL[t.status]?.label ?? t.status}</Badge>
                    </TableCell>
                    <TableCell className="tabular-nums">{t.version}</TableCell>
                    <TableCell className="tabular-nums">{formatDateTime(t.updated_at)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </Card>
    </>
  )
}
