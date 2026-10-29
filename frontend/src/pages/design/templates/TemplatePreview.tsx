/**
 * Pré-visualização APROXIMADA no navegador (posição na grade 24×24, cores, texto com
 * variáveis de teste). O resultado oficial é sempre o PNG/JPG renderizado no servidor.
 * Texto é exibido como texto puro (sem HTML).
 */
import type { CSSProperties } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchBinary } from '@/components/admin/binary'
import { useObjectUrl } from '@/components/admin/hooks'
import { cn } from '@/lib/utils'
import { CANVAS, GRID, previewText, type EditorState, type Orientation } from './templateSpec'

function AssetImage({ assetId, fit }: { assetId: string; fit: 'contain' | 'cover' }) {
  const q = useQuery({
    queryKey: ['design', 'template-asset', assetId],
    queryFn: () => fetchBinary(`/templates/assets/${assetId}/file`).then((r) => r.blob),
    enabled: !!assetId,
    staleTime: Infinity,
    retry: false,
  })
  const url = useObjectUrl(q.data)
  if (!assetId) return <span className="flex size-full items-center justify-center text-[1.2cqw] text-text-muted">Imagem não enviada</span>
  return url ? <img src={url} alt="" className={cn('size-full', fit === 'cover' ? 'object-cover' : 'object-contain')} /> : null
}

export function TemplatePreview({
  state,
  orientation,
  values,
  showGrid,
  selected,
}: {
  state: EditorState
  orientation: Orientation
  values: Record<string, string>
  showGrid: boolean
  selected?: string | null
}) {
  const canvas = CANVAS[orientation]
  const cell = 100 / GRID
  return (
    <div
      className={cn('relative w-full overflow-hidden rounded-lg border border-border', orientation === 'VERTICAL' && 'mx-auto max-w-[360px]')}
      style={{ aspectRatio: `${canvas.width} / ${canvas.height}`, background: state.background, containerType: 'inline-size' }}
      role="img"
      aria-label={`Pré-visualização aproximada ${canvas.width}×${canvas.height} com ${state.layers.length} camada(s)`}
    >
      {state.layers.map((l) => {
        const style: CSSProperties = {
          left: `${l.box.col * cell}%`,
          top: `${l.box.row * cell}%`,
          width: `${l.box.col_span * cell}%`,
          height: `${l.box.row_span * cell}%`,
        }
        const ring = selected === l.uid ? 'outline outline-2 outline-offset-[-2px] outline-[rgb(var(--focus))]' : ''
        if (l.type === 'rect')
          return (
            <div
              key={l.uid}
              className={cn('absolute', ring)}
              style={{ ...style, background: l.color, opacity: l.opacity / 100, borderRadius: `${(l.radius_px / canvas.width) * 100}cqw` }}
            />
          )
        if (l.type === 'image')
          return (
            <div key={l.uid} className={cn('absolute', ring)} style={style}>
              <AssetImage assetId={l.asset_id} fit={l.fit} />
            </div>
          )
        const text = previewText(l.text, state.variables, values)
        return (
          <div
            key={l.uid}
            className={cn('absolute flex overflow-hidden p-[0.2cqw]', ring)}
            style={{
              ...style,
              color: l.color,
              fontSize: `${(l.size_px / canvas.width) * 100}cqw`,
              lineHeight: 1.15,
              fontWeight: l.font.endsWith('bold') ? 700 : 400,
              fontFamily: l.font.startsWith('dejavu-serif') ? 'Georgia, serif' : l.font === 'dejavu-mono' ? 'monospace' : 'Manrope, sans-serif',
              textAlign: l.align,
              alignItems: l.valign === 'top' ? 'flex-start' : l.valign === 'bottom' ? 'flex-end' : 'center',
              justifyContent: l.align === 'left' ? 'flex-start' : l.align === 'right' ? 'flex-end' : 'center',
              textTransform: l.uppercase ? 'uppercase' : undefined,
            }}
          >
            <span className="block w-full whitespace-pre-line break-words" style={{ display: '-webkit-box', WebkitLineClamp: l.max_lines, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>
              {text}
            </span>
          </div>
        )
      })}
      {showGrid ? (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-0"
          style={{
            backgroundImage:
              'linear-gradient(to right, rgb(91 206 255 / 0.25) 1px, transparent 1px), linear-gradient(to bottom, rgb(91 206 255 / 0.25) 1px, transparent 1px)',
            backgroundSize: `${cell}% ${cell}%`,
          }}
        />
      ) : null}
      {showGrid ? (
        <div aria-hidden="true" className="pointer-events-none absolute inset-[5%] border border-dashed border-[rgb(246_198_106/0.6)]" title="Margem de segurança de 5%" />
      ) : null}
    </div>
  )
}
