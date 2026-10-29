import { useEffect, useState } from 'react'

/** Valor com atraso (busca por digitação) — um único setTimeout por mudança, sem polling. */
export function useDebounced<T>(value: T, delayMs = 300): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setV(value), delayMs)
    return () => clearTimeout(t)
  }, [value, delayMs])
  return v
}
