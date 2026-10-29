import { MutationCache, QueryCache, QueryClient } from '@tanstack/react-query'
import { isSetupRequiredError } from '@/features/auth/setup'
import { isApiError } from './errors'

/**
 * Erros globais de primeiro acesso (SPEC AÇÃO 003 §11.9): 403 `PASSWORD_CHANGE_REQUIRED`,
 * `MFA_SETUP_REQUIRED` ou `FINANCIAL_PASSWORD_SETUP_REQUIRED` vindos de QUALQUER query/mutation
 * recarregam o `/me` e levam ao assistente. O handler é registrado pelo authStore (evita ciclo de
 * importação) e fica fora de `lib/http.ts` de propósito.
 */
type SetupRequiredHandler = (code: string) => void
let setupRequiredHandler: SetupRequiredHandler | null = null
export function setSetupRequiredHandler(fn: SetupRequiredHandler | null) {
  setupRequiredHandler = fn
}

function onGlobalError(error: unknown) {
  if (isSetupRequiredError(error) && isApiError(error)) setupRequiredHandler?.(error.code)
}

/**
 * TanStack Query — sem polling (P001 §70, E2 #826):
 * - NENHUM `refetchInterval` (proibido; teste de arquitetura em src/test/no-polling.test.ts).
 * - Dados mudam por eventos WebSocket (RealtimeProvider invalida as queries afetadas)
 *   e, na reconexão, um único refetch das queries ativas.
 * - `refetchOnWindowFocus`/`refetchOnReconnect` desligados: o WS cobre esses casos.
 * - `staleTime` de 5 min: suficiente para navegar entre páginas sem refazer requisições,
 *   já que mudanças relevantes chegam por evento.
 */
export function createQueryClient() {
  return new QueryClient({
    queryCache: new QueryCache({ onError: onGlobalError }),
    mutationCache: new MutationCache({ onError: onGlobalError }),
    defaultOptions: {
      queries: {
        staleTime: 5 * 60 * 1000,
        gcTime: 15 * 60 * 1000,
        refetchOnWindowFocus: false,
        refetchOnReconnect: false,
        retry: (failureCount, error) => {
          if (isApiError(error) && error.status >= 400 && error.status < 500) return false
          return failureCount < 2
        },
        retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 15_000),
      },
      mutations: {
        retry: false,
      },
    },
  })
}

export const queryClient = createQueryClient()
