/**
 * Verificação de comprovante de veiculação (REQ-REL-004): envie o PDF (o servidor confere o
 * SHA-256 e as assinaturas ES256 da instância) ou consulte pelo código para verificação
 * offline com a chave pública publicada. Não é certificado ICP-Brasil.
 */
import * as React from 'react'
import { useMutation } from '@tanstack/react-query'
import { CheckCircle2, FileSearch, Search, XCircle } from 'lucide-react'
import { PageHeader } from '@/components/layout/page-meta'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { FileDropzone } from '@/components/patterns/file-dropzone'
import { DetailList } from '@/components/finance/detail-list'
import { formatIsoDate } from '@/components/finance/period'
import { errorMessage, isApiError } from '@/lib/errors'
import { formatDateTime, formatNumber } from '@/lib/format'
import { reportsApi } from './api'

const CODE = /^CV-[A-Z0-9]{8}$/

export default function ProofVerificationPage() {
  const [file, setFile] = React.useState<File | null>(null)
  const [code, setCode] = React.useState('')
  const [codeError, setCodeError] = React.useState<string | null>(null)
  const verify = useMutation({ mutationFn: (f: File) => reportsApi.verifyPdf(f) })
  const lookup = useMutation({ mutationFn: (c: string) => reportsApi.verification(c) })
  const r = verify.data

  return (
    <div className="flex flex-col gap-6">
      <PageHeader title="Verificar comprovante" description="Autenticidade de comprovantes de veiculação emitidos pela plataforma" />
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2 xl:gap-6">
        <Card>
          <CardHeader>
            <div className="flex flex-col gap-1">
              <CardTitle>Enviar o PDF</CardTitle>
              <CardDescription>O arquivo é comparado com o registrado (SHA-256) e as assinaturas são conferidas.</CardDescription>
            </div>
            <FileSearch aria-hidden="true" className="size-5 text-text-muted" />
          </CardHeader>
          <FileDropzone
            label="Comprovante em PDF"
            accept={['application/pdf']}
            maxSizeBytes={50 * 1024 * 1024}
            multiple={false}
            requirements="Somente o PDF original do comprovante (até 50 MB). O arquivo não é guardado."
            items={file ? [{ id: 'proof', file, status: verify.isPending ? 'uploading' : r ? (r.valid ? 'ready' : 'error') : 'selected' }] : []}
            onFilesAccepted={(fs) => {
              setFile(fs[0] ?? null)
              verify.reset()
            }}
            onRemove={() => {
              setFile(null)
              verify.reset()
            }}
          />
          <Button className="self-start" disabled={!file} loading={verify.isPending} loadingText="Verificando…" onClick={() => file && verify.mutate(file)}>
            Verificar
          </Button>
          {verify.error ? (
            <Alert variant="danger">
              <AlertDescription>{errorMessage(verify.error)}</AlertDescription>
            </Alert>
          ) : null}
          {r ? (
            <Alert variant={r.valid ? 'success' : 'danger'} role="status">
              {r.valid ? <CheckCircle2 aria-hidden="true" /> : <XCircle aria-hidden="true" />}
              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <AlertTitle>{r.valid ? `Comprovante ${r.code} autêntico` : 'Comprovante NÃO reconhecido'}</AlertTitle>
                <AlertDescription className="flex flex-col gap-2">
                  {r.valid ? (
                    <DetailList
                      columns={1}
                      items={[
                        { label: 'Emitido em', value: r.issued_at ? formatDateTime(r.issued_at) : '—' },
                        { label: 'Período', value: r.period ? `${formatIsoDate(r.period.start)} a ${formatIsoDate(r.period.end)}` : '—' },
                        { label: 'Exibições válidas', value: formatNumber(r.total_valid_plays ?? null), numeric: true },
                        { label: 'Chave (kid)', value: <span className="font-mono text-xs">{r.kid}</span> },
                      ]}
                    />
                  ) : (
                    <span>{r.reason ?? 'O arquivo foi alterado ou não foi emitido por esta plataforma.'}</span>
                  )}
                  <span className="break-all font-mono text-xs">SHA-256 do arquivo: {r.pdf_sha256}</span>
                </AlertDescription>
              </div>
            </Alert>
          ) : null}
        </Card>
        <Card>
          <CardHeader>
            <div className="flex flex-col gap-1">
              <CardTitle>Consultar pelo código</CardTitle>
              <CardDescription>Dados para verificação offline: hashes, assinaturas e chave pública da instância.</CardDescription>
            </div>
          </CardHeader>
          <form
            className="flex flex-wrap items-end gap-3"
            noValidate
            onSubmit={(e) => {
              e.preventDefault()
              const c = code.trim().toUpperCase()
              if (!CODE.test(c)) {
                setCodeError('Use o código impresso no comprovante (ex.: CV-ABCD2345).')
                return
              }
              setCodeError(null)
              lookup.mutate(c)
            }}
          >
            <Field invalid={!!codeError} className="w-full max-w-xs">
              <FieldLabel required>Código</FieldLabel>
              <Input value={code} onChange={(e) => setCode(e.target.value)} className="font-mono uppercase" />
              <FieldError>{codeError}</FieldError>
            </Field>
            <Button type="submit" variant="secondary" loading={lookup.isPending} loadingText="Consultando…">
              <Search aria-hidden="true" />
              Consultar
            </Button>
          </form>
          {lookup.error ? (
            <Alert variant="danger">
              <AlertDescription>{isApiError(lookup.error) && lookup.error.status === 404 ? 'Código não encontrado.' : errorMessage(lookup.error)}</AlertDescription>
            </Alert>
          ) : null}
          {lookup.data ? (
            <div className="flex flex-col gap-3" role="status">
              <DetailList
                columns={1}
                items={[
                  { label: 'Código', value: <span className="font-mono">{lookup.data.code}</span> },
                  { label: 'Emitido em', value: lookup.data.issued_at ? formatDateTime(lookup.data.issued_at) : '—' },
                  { label: 'Algoritmo', value: lookup.data.alg },
                  { label: 'Chave (kid)', value: <span className="font-mono text-xs">{lookup.data.kid}</span> },
                  { label: 'SHA-256 do conteúdo', value: <span className="break-all font-mono text-xs">{lookup.data.payload_sha256}</span> },
                  { label: 'SHA-256 do PDF', value: <span className="break-all font-mono text-xs">{lookup.data.pdf_sha256}</span> },
                  { label: 'Chaves públicas', value: <span className="font-mono text-xs">{lookup.data.public_keys_url}</span> },
                ]}
              />
              <Field>
                <FieldDescription>{lookup.data.note}</FieldDescription>
              </Field>
            </div>
          ) : null}
        </Card>
      </div>
    </div>
  )
}
