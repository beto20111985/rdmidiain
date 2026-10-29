import { useCallback, useState } from 'react'

export interface GeoReading {
  lat: number
  lng: number
  accuracy_m: number | null
}

const MESSAGES: Record<number, string> = {
  1: 'Permissão de localização negada. Libere a localização para este site nas configurações do navegador.',
  2: 'Não foi possível obter a localização (sinal de GPS indisponível).',
  3: 'A localização demorou demais. Tente novamente em local aberto.',
}

/**
 * GPS do navegador SOMENTE por ação explícita do usuário (botão) — o navegador pede permissão.
 * Leitura única (getCurrentPosition), sem watchPosition/polling.
 */
export function useBrowserGeolocation() {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const supported = typeof navigator !== 'undefined' && !!navigator.geolocation

  const request = useCallback((): Promise<GeoReading | null> => {
    if (!supported) {
      setError('Este navegador não oferece localização por GPS.')
      return Promise.resolve(null)
    }
    setPending(true)
    setError(null)
    return new Promise((resolve) => {
      navigator.geolocation.getCurrentPosition(
        (pos) => {
          setPending(false)
          resolve({
            lat: Number(pos.coords.latitude.toFixed(7)),
            lng: Number(pos.coords.longitude.toFixed(7)),
            accuracy_m: Number.isFinite(pos.coords.accuracy) ? Math.round(pos.coords.accuracy) : null,
          })
        },
        (err) => {
          setPending(false)
          setError(MESSAGES[err.code] ?? 'Não foi possível obter a localização.')
          resolve(null)
        },
        { enableHighAccuracy: true, timeout: 20000, maximumAge: 0 },
      )
    })
  }, [supported])

  return { request, pending, error, supported }
}
