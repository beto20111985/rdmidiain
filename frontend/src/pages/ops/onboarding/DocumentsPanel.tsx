/**
 * Documentos do cadastro (opcionais — #1006): contrato social/cartão CNPJ, identidade, comprovante
 * de endereço. Valida tipo (PDF/JPEG/PNG) e tamanho (10 MB) ANTES do envio; o servidor confere o
 * tipo real pelos bytes. Progresso real quando disponível.
 */
import * as React from 'react'
import { FileDropzone, type UploadItem } from '@/components/patterns/file-dropzone'
import { Field, FieldLabel } from '@/components/ui/field'
import { OptionSelect } from '@/components/ops/filter-select'
import { errorMessage } from '@/lib/errors'
import { formatBytes, formatDateTime } from '@/lib/format'
import { DOCUMENT_KIND_LABEL, labelOf } from '@/pages/ops/api/labels'
import type { OnboardingDocument } from '@/pages/ops/api/types'
import { DOC_ACCEPT, DOC_MAX_BYTES } from './stepRules'

export function DocumentsPanel({
  documents,
  upload,
  disabled,
  onUploaded,
}: {
  documents: OnboardingDocument[]
  upload: (kind: string, file: File, onProgress: (f: number) => void) => Promise<unknown>
  disabled?: boolean
  onUploaded?: () => void
}) {
  const [kind, setKind] = React.useState<string | null>('COMPANY_REGISTRATION')
  const [items, setItems] = React.useState<UploadItem[]>([])
  const seq = React.useRef(0)

  const send = async (item: UploadItem, k: string) => {
    setItems((p) => p.map((i) => (i.id === item.id ? { ...i, status: 'uploading', progress: 0, error: undefined } : i)))
    try {
      await upload(k, item.file, (f) => setItems((p) => p.map((i) => (i.id === item.id ? { ...i, progress: f } : i))))
      setItems((p) => p.map((i) => (i.id === item.id ? { ...i, status: 'ready', progress: 1 } : i)))
      onUploaded?.()
    } catch (e) {
      setItems((p) => p.map((i) => (i.id === item.id ? { ...i, status: 'error', error: errorMessage(e) } : i)))
    }
  }

  return (
    <div className="flex flex-col gap-5">
      {documents.length ? (
        <ul className="m-0 flex list-none flex-col divide-y divide-border p-0" aria-label="Documentos enviados">
          {documents.map((d) => (
            <li key={d.id} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
              <span className="font-semibold text-text">{labelOf(DOCUMENT_KIND_LABEL, d.kind)}</span>
              <span className="text-text-muted">
                {d.content_type} · {formatBytes(d.size_bytes)}
                {d.created_at ? ` · ${formatDateTime(d.created_at)}` : ''}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="m-0 text-sm text-text-muted">Nenhum documento enviado. Os documentos são opcionais e agilizam a análise.</p>
      )}
      {!disabled ? (
        <>
          <Field className="max-w-sm">
            <FieldLabel>Tipo do documento</FieldLabel>
            <OptionSelect
              value={kind}
              onChange={setKind}
              options={Object.entries(DOCUMENT_KIND_LABEL).map(([value, label]) => ({ value, label }))}
            />
          </Field>
          <FileDropzone
            accept={DOC_ACCEPT}
            maxSizeBytes={DOC_MAX_BYTES}
            multiple={false}
            label="Selecionar documento"
            requirements="PDF, JPEG ou PNG, até 10 MB. Arquivos com conteúdo diferente da extensão são recusados."
            items={items}
            onFilesAccepted={(files) => {
              const k = kind ?? 'OTHER'
              const created = files.map((file) => ({ id: `doc-${++seq.current}`, file, status: 'selected' as const }))
              setItems((p) => [...p, ...created])
              created.forEach((it) => void send(it, k))
            }}
            onRetry={(id) => {
              const it = items.find((i) => i.id === id)
              if (it) void send(it, kind ?? 'OTHER')
            }}
            onRemove={(id) => setItems((p) => p.filter((i) => i.id !== id))}
          />
        </>
      ) : null}
    </div>
  )
}
