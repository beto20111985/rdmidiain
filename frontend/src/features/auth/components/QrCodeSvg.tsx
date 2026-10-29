/**
 * QR code gerado LOCALMENTE (biblioteca `qrcode`, sem serviço de terceiros e sem data URL):
 * a matriz é desenhada como SVG. Sempre escuro sobre claro (leitura das câmeras), nos dois temas.
 */
import * as React from 'react'
import QRCode from 'qrcode'

export function QrCodeSvg({ value, size = 208, label }: { value: string; size?: number; label: string }) {
  const { path, dim } = React.useMemo(() => {
    const qr = QRCode.create(value, { errorCorrectionLevel: 'M' })
    const n = qr.modules.size
    const quiet = 4
    let d = ''
    for (let r = 0; r < n; r++) {
      for (let c = 0; c < n; c++) {
        if (qr.modules.get(r, c)) d += `M${c + quiet},${r + quiet}h1v1h-1z`
      }
    }
    return { path: d, dim: n + quiet * 2 }
  }, [value])
  return (
    <svg
      role="img"
      aria-label={label}
      width={size}
      height={size}
      viewBox={`0 0 ${dim} ${dim}`}
      shapeRendering="crispEdges"
      className="rounded-lg"
    >
      <rect width={dim} height={dim} fill="#ffffff" />
      <path d={path} fill="#05070a" />
    </svg>
  )
}
