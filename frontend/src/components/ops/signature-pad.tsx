/**
 * Captura de assinatura (desenho com mouse/toque/caneta) → PNG. Alternativa acessível:
 * enviar uma imagem JPEG/PNG da assinatura (teclado/leitor de tela). Não usa timers.
 */
import * as React from 'react'
import { Eraser } from 'lucide-react'
import { Button } from '@/components/ui/button'

export interface SignaturePadHandle {
  toBlob: () => Promise<Blob | null>
  clear: () => void
  isEmpty: () => boolean
}

export const SignaturePad = React.forwardRef<SignaturePadHandle, { label: string; onChange?: (empty: boolean) => void }>(
  ({ label, onChange }, ref) => {
    const canvasRef = React.useRef<HTMLCanvasElement>(null)
    const drawing = React.useRef(false)
    const [empty, setEmpty] = React.useState(true)

    const ctx = () => {
      const c = canvasRef.current
      const g = c?.getContext('2d')
      if (g) {
        g.lineWidth = 2.5
        g.lineCap = 'round'
        g.lineJoin = 'round'
        g.strokeStyle = '#05070a'
      }
      return g
    }

    const pos = (e: React.PointerEvent<HTMLCanvasElement>) => {
      const r = e.currentTarget.getBoundingClientRect()
      const c = e.currentTarget
      return { x: ((e.clientX - r.left) * c.width) / r.width, y: ((e.clientY - r.top) * c.height) / r.height }
    }

    const markDirty = () => {
      if (empty) {
        setEmpty(false)
        onChange?.(false)
      }
    }

    React.useImperativeHandle(ref, () => ({
      toBlob: () =>
        new Promise((resolve) => {
          const c = canvasRef.current
          if (!c || empty) return resolve(null)
          // fundo branco para o PNG (assinatura escura legível em qualquer visualizador)
          const out = document.createElement('canvas')
          out.width = c.width
          out.height = c.height
          const g = out.getContext('2d')
          if (!g) return resolve(null)
          g.fillStyle = '#ffffff'
          g.fillRect(0, 0, out.width, out.height)
          g.drawImage(c, 0, 0)
          out.toBlob((b) => resolve(b), 'image/png')
        }),
      clear: () => {
        const c = canvasRef.current
        c?.getContext('2d')?.clearRect(0, 0, c.width, c.height)
        setEmpty(true)
        onChange?.(true)
      },
      isEmpty: () => empty,
    }))

    return (
      <div className="flex flex-col gap-2">
        <canvas
          ref={canvasRef}
          width={600}
          height={200}
          role="img"
          aria-label={`${label}. Desenhe a assinatura com o dedo, caneta ou mouse.`}
          className="h-40 w-full touch-none rounded-lg border border-control-border bg-white"
          onPointerDown={(e) => {
            const g = ctx()
            if (!g) return
            e.currentTarget.setPointerCapture?.(e.pointerId)
            drawing.current = true
            const p = pos(e)
            g.beginPath()
            g.moveTo(p.x, p.y)
          }}
          onPointerMove={(e) => {
            if (!drawing.current) return
            const g = ctx()
            if (!g) return
            const p = pos(e)
            g.lineTo(p.x, p.y)
            g.stroke()
            markDirty()
          }}
          onPointerUp={() => {
            drawing.current = false
          }}
          onPointerLeave={() => {
            drawing.current = false
          }}
        />
        <div className="flex items-center justify-between gap-2">
          <span className="text-xs text-text-muted">{empty ? 'Nenhuma assinatura desenhada.' : 'Assinatura desenhada.'}</span>
          <Button
            type="button"
            variant="tertiary"
            onClick={() => {
              const c = canvasRef.current
              c?.getContext('2d')?.clearRect(0, 0, c.width, c.height)
              setEmpty(true)
              onChange?.(true)
            }}
            disabled={empty}
          >
            <Eraser aria-hidden="true" />
            Limpar assinatura
          </Button>
        </div>
      </div>
    )
  },
)
SignaturePad.displayName = 'SignaturePad'
