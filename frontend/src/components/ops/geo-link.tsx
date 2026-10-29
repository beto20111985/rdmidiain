/**
 * Localização sem mapa embutido: a CSP do painel (img-src 'self' data: blob:) bloqueia tiles de
 * mapas externos e nenhuma biblioteca de mapa sem chave e sem tiles externos está disponível.
 * Mostra coordenadas, origem, precisão e um link "Abrir no mapa" (OpenStreetMap, nova aba).
 */
import { ExternalLink, MapPin } from 'lucide-react'
import { EMPTY_VALUE, formatNumber } from '@/lib/format'
import { osmUrl } from './utils'

export function GeoLink({
  lat,
  lng,
  source,
  accuracyM,
  label = 'Abrir no mapa',
}: {
  lat: number | null | undefined
  lng: number | null | undefined
  source?: string | null
  accuracyM?: number | null
  label?: string
}) {
  if (lat === null || lat === undefined || lng === null || lng === undefined) {
    return <span className="text-text-muted">{EMPTY_VALUE} (sem coordenadas)</span>
  }
  return (
    <div className="flex flex-col gap-1">
      <span className="inline-flex items-center gap-1.5 tabular-nums text-text">
        <MapPin aria-hidden="true" className="size-4 text-link" />
        {formatNumber(lat, { minimumFractionDigits: 6, maximumFractionDigits: 6 })},{' '}
        {formatNumber(lng, { minimumFractionDigits: 6, maximumFractionDigits: 6 })}
      </span>
      <span className="text-xs text-text-muted">
        {source === 'GPS' ? 'Capturada por GPS' : source === 'MANUAL' ? 'Informada manualmente' : 'Origem não informada'}
        {accuracyM !== null && accuracyM !== undefined ? ` · precisão ≈ ${formatNumber(accuracyM)} m` : ''}
      </span>
      <a
        href={osmUrl(lat, lng)}
        target="_blank"
        rel="noopener noreferrer"
        className="inline-flex min-h-11 w-fit items-center gap-1.5 text-sm font-semibold text-link underline-offset-4 hover:underline"
      >
        {label}
        <ExternalLink aria-hidden="true" className="size-4" />
        <span className="sr-only">(abre o OpenStreetMap em nova aba)</span>
      </a>
    </div>
  )
}
