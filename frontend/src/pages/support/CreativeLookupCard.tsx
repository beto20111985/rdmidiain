/**
 * Busca EXATA pelo ID do anúncio exibido na TV (GET /support/creatives/{code}) — somente
 * equipe RD com `support.manage`. Mostra criativo, anunciante, campanhas e chamados ligados
 * ao código, com atalho para abrir chamado. Prefixo/curinga não são aceitos (400).
 */
import * as React from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Search } from 'lucide-react'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Field, FieldError, FieldLabel } from '@/components/ui/field'
import { http } from '@/lib/http'
import { errorMessage, isApiError } from '@/lib/errors'
import { formatDateTime } from '@/lib/format'
import { formatCalendarDate } from '@/components/admin/dates'
import { TICKET_STATUS, lookup } from '@/components/admin/labels'
import { KeyValueList, ToneBadge } from '@/components/admin/ui'
import { CREATIVE_CODE_RE, normalizeCreativeCode, supportNewTicketPath } from '@/components/admin/support-links'
import type { CreativeLookup } from './types'

export function CreativeLookupCard() {
  const [input, setInput] = React.useState('')
  const [code, setCode] = React.useState<string | null>(null)
  const [problem, setProblem] = React.useState<string | null>(null)
  const q = useQuery({
    queryKey: ['support', 'creative', code],
    queryFn: () => http.get<CreativeLookup>(`/support/creatives/${encodeURIComponent(code!)}`),
    enabled: !!code,
    retry: false,
  })
  const search = () => {
    const n = normalizeCreativeCode(input)
    if (!n || !CREATIVE_CODE_RE.test(n)) {
      setProblem('Digite o código completo no formato RD-XXXXXX (busca exata).')
      return
    }
    setProblem(null)
    setCode(n)
  }
  const notFound = isApiError(q.error) && q.error.status === 404
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-col gap-1">
          <CardTitle>Localizar anúncio pelo código da tela</CardTitle>
          <CardDescription>Busca exata pelo ID RD-XXXXXX que aparece no anúncio (sem prefixo ou curinga).</CardDescription>
        </div>
      </CardHeader>
      <form
        noValidate
        className="flex flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault()
          search()
        }}
      >
        <Field className="w-full sm:w-56" invalid={!!problem}>
          <FieldLabel>Código do anúncio</FieldLabel>
          <Input value={input} onChange={(e) => setInput(e.target.value.toUpperCase())} placeholder="RD-XXXXXX" className="font-mono" spellCheck={false} autoComplete="off" />
          <FieldError>{problem}</FieldError>
        </Field>
        <Button type="submit" variant="secondary" loading={q.isFetching}>
          <Search aria-hidden="true" />
          Buscar
        </Button>
      </form>
      {code && q.error ? (
        <Alert variant={notFound ? 'neutral' : 'danger'}>
          <AlertDescription className="text-text">{notFound ? `Nenhum anúncio com o código ${code}.` : errorMessage(q.error)}</AlertDescription>
        </Alert>
      ) : null}
      {q.data ? (
        <div className="flex flex-col gap-4 rounded-lg border border-border p-4">
          <KeyValueList
            items={[
              { label: 'Anúncio', value: `${q.data.creative.code} — ${q.data.creative.name}` },
              { label: 'Situação do anúncio', value: q.data.creative.status },
              {
                label: 'Anunciante',
                value: (
                  <Link to={`/empresas/${q.data.advertiser.org_id}`} className="font-semibold text-link underline-offset-4 hover:underline">
                    {q.data.advertiser.name ?? 'Empresa'}
                  </Link>
                ),
              },
              {
                label: 'Campanhas',
                value: q.data.campaigns.length ? (
                  <ul className="m-0 flex list-none flex-col gap-1 p-0">
                    {q.data.campaigns.map((c) => (
                      <li key={c.id}>
                        <Link to={`/campanhas/${c.id}`} className="text-link underline-offset-4 hover:underline">
                          {c.name}
                        </Link>{' '}
                        <span className="text-xs text-text-muted">
                          {c.status} · {formatCalendarDate(c.start_date)} a {formatCalendarDate(c.end_date)}
                        </span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  'Nenhuma'
                ),
                wide: true,
              },
              {
                label: 'Chamados com este código',
                value: q.data.tickets.length ? (
                  <ul className="m-0 flex list-none flex-col gap-1 p-0">
                    {q.data.tickets.map((t) => (
                      <li key={t.id} className="flex flex-wrap items-center gap-2">
                        <Link to={`/suporte/${t.id}`} className="text-link underline-offset-4 hover:underline">
                          {t.number}
                        </Link>
                        <ToneBadge value={lookup(TICKET_STATUS, t.status)} />
                        <span className="text-xs text-text-muted">{formatDateTime(t.created_at)}</span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  'Nenhum'
                ),
                wide: true,
              },
            ]}
          />
          <Button asChild variant="secondary" className="w-fit">
            <Link to={supportNewTicketPath('rd', { code: q.data.creative.code })}>Abrir chamado com este código</Link>
          </Button>
        </div>
      ) : null}
    </Card>
  )
}
