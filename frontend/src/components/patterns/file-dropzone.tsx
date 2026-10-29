/**
 * Área de envio acessível (DS §10 Upload de mídia):
 * - arrastar e soltar + botão equivalente "Selecionar arquivos" (teclado/leitor de tela);
 * - valida tipo e tamanho ANTES do envio, com erro por arquivo;
 * - lista nome, tipo, tamanho e progresso REAL (use lib/upload.ts → onProgress);
 * - estados por item: selecionado, enviando, processando, pronto, erro (com tentar novamente).
 * O componente é controlado: a página guarda `items` e executa o envio.
 */
import * as React from 'react'
import { AlertCircle, CheckCircle2, FileVideo, Image as ImageIcon, Loader2, RotateCcw, Upload, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Progress } from '@/components/ui/progress'
import { cn } from '@/lib/utils'
import { formatBytes } from '@/lib/format'
import { validateFile } from './file-validation'

export type UploadItemStatus = 'selected' | 'uploading' | 'processing' | 'ready' | 'error'

export interface UploadItem {
  id: string
  file: File
  status: UploadItemStatus
  /** 0–1 durante "uploading". */
  progress?: number
  error?: string
  /** Metadados extraídos (dimensões, duração) quando disponíveis. */
  meta?: string
}

export interface FileDropzoneProps {
  /** Ex.: ['image/jpeg','image/png','video/mp4'] */
  accept: string[]
  maxSizeBytes: number
  multiple?: boolean
  /** Requisitos do player efetivamente implementado (texto visível). */
  requirements: React.ReactNode
  items: UploadItem[]
  onFilesAccepted: (files: File[]) => void
  onRetry?: (id: string) => void
  onRemove?: (id: string) => void
  disabled?: boolean
  label?: string
}

const STATUS_TEXT: Record<UploadItemStatus, string> = {
  selected: 'Selecionado',
  uploading: 'Enviando',
  processing: 'Processando',
  ready: 'Pronto',
  error: 'Erro',
}

export function FileDropzone({
  accept,
  maxSizeBytes,
  multiple = true,
  requirements,
  items,
  onFilesAccepted,
  onRetry,
  onRemove,
  disabled,
  label = 'Enviar arquivos',
}: FileDropzoneProps) {
  const inputRef = React.useRef<HTMLInputElement>(null)
  const [dragging, setDragging] = React.useState(false)
  const [rejections, setRejections] = React.useState<Array<{ name: string; reason: string }>>([])
  const reqId = React.useId()

  const handleFiles = (list: FileList | null) => {
    if (!list || disabled) return
    const files = Array.from(list).slice(0, multiple ? undefined : 1)
    const ok: File[] = []
    const bad: Array<{ name: string; reason: string }> = []
    for (const f of files) {
      const reason = validateFile(f, accept, maxSizeBytes)
      if (reason) bad.push({ name: f.name, reason })
      else ok.push(f)
    }
    setRejections(bad)
    if (ok.length) onFilesAccepted(ok)
  }

  return (
    <div className="flex flex-col gap-4">
      <div
        onDragOver={(e) => {
          e.preventDefault()
          if (!disabled) setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault()
          setDragging(false)
          handleFiles(e.dataTransfer.files)
        }}
        className={cn(
          'flex flex-col items-center justify-center gap-3 rounded-xl border-2 border-dashed border-control-border bg-field px-6 py-8 text-center transition-colors duration-fast ease-rd',
          dragging && 'border-focus bg-surface-hover',
          disabled && 'opacity-70',
        )}
      >
        <Upload aria-hidden="true" className="size-8 text-link" />
        <p className="text-sm font-semibold text-text">Arraste os arquivos para cá ou</p>
        <Button variant="secondary" onClick={() => inputRef.current?.click()} disabled={disabled} aria-describedby={reqId}>
          Selecionar arquivos
        </Button>
        <input
          ref={inputRef}
          type="file"
          className="sr-only"
          tabIndex={-1}
          aria-label={label}
          accept={accept.join(',')}
          multiple={multiple}
          disabled={disabled}
          onChange={(e) => {
            handleFiles(e.target.files)
            e.target.value = ''
          }}
        />
        <div id={reqId} className="text-xs text-text-muted">
          {requirements}
        </div>
      </div>

      {rejections.length ? (
        <ul role="alert" className="m-0 flex list-none flex-col gap-1 p-0 text-sm">
          {rejections.map((r) => (
            <li key={r.name} className="flex items-start gap-2 text-danger">
              <AlertCircle aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
              <span>
                <strong className="font-semibold">{r.name}</strong>: {r.reason}
              </span>
            </li>
          ))}
        </ul>
      ) : null}

      {items.length ? (
        <ul aria-label="Arquivos" className="m-0 flex list-none flex-col gap-2 p-0">
          {items.map((it) => {
            const Icon = it.file.type.startsWith('video') ? FileVideo : ImageIcon
            const pct = Math.round((it.progress ?? 0) * 100)
            return (
              <li key={it.id} className="flex flex-col gap-2 rounded-lg border border-border bg-surface p-3">
                <div className="flex items-center gap-3">
                  <Icon aria-hidden="true" className="size-5 shrink-0 text-text-muted" />
                  <div className="flex min-w-0 flex-1 flex-col">
                    <span className="truncate text-sm font-semibold text-text">{it.file.name}</span>
                    <span className="text-xs text-text-muted">
                      {it.file.type || 'tipo desconhecido'} · {formatBytes(it.file.size)}
                      {it.meta ? ` · ${it.meta}` : ''}
                    </span>
                  </div>
                  <span
                    className={cn(
                      'inline-flex shrink-0 items-center gap-1 text-xs font-semibold',
                      it.status === 'error' ? 'text-danger' : it.status === 'ready' ? 'text-success' : 'text-text-muted',
                    )}
                  >
                    {it.status === 'ready' ? <CheckCircle2 aria-hidden="true" className="size-4" /> : null}
                    {it.status === 'processing' || it.status === 'uploading' ? (
                      <Loader2 aria-hidden="true" className="size-4 animate-spin" />
                    ) : null}
                    {it.status === 'error' ? <AlertCircle aria-hidden="true" className="size-4" /> : null}
                    {STATUS_TEXT[it.status]}
                    {it.status === 'uploading' ? ` ${pct}%` : ''}
                  </span>
                  {it.status === 'error' && onRetry ? (
                    <Button variant="ghost" size="icon" aria-label={`Tentar enviar ${it.file.name} novamente`} onClick={() => onRetry(it.id)}>
                      <RotateCcw aria-hidden="true" />
                    </Button>
                  ) : null}
                  {onRemove && it.status !== 'uploading' ? (
                    <Button variant="ghost" size="icon" aria-label={`Remover ${it.file.name}`} onClick={() => onRemove(it.id)}>
                      <X aria-hidden="true" />
                    </Button>
                  ) : null}
                </div>
                {it.status === 'uploading' ? <Progress value={pct} aria-label={`Envio de ${it.file.name}`} /> : null}
                {it.status === 'error' && it.error ? <p className="text-xs text-danger">{it.error}</p> : null}
              </li>
            )
          })}
        </ul>
      ) : null}
    </div>
  )
}
