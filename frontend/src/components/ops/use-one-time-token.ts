import { useEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

/**
 * Lê `?token=` da URL UMA vez, remove-o da barra de endereço (replace) e o mantém só em memória
 * nesta aba — nunca em storage nem na URL das chamadas à API (vai no header pedido pelo backend).
 */
export function useOneTimeToken(param = 'token'): string | null {
  const location = useLocation()
  const navigate = useNavigate()
  const [token] = useState(() => new URLSearchParams(location.search).get(param))
  useEffect(() => {
    const sp = new URLSearchParams(location.search)
    if (sp.has(param)) {
      sp.delete(param)
      const rest = sp.toString()
      navigate(`${location.pathname}${rest ? `?${rest}` : ''}`, { replace: true })
    }
  }, [location.pathname, location.search, navigate, param])
  return token
}
