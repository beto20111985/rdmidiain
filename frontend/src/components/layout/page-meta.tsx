/**
 * Metadados da página exibidos pelo Header (único <h1> da página — A3: h1 duplicado).
 *
 * Em cada página:
 *   <PageHeader title="Telas" description="12 telas · 2 offline" actions={<Button>Adicionar tela</Button>} />
 * O título padrão vem de routes.ts; `title` sobrescreve (ex.: nome do registro no detalhe).
 * `actions` = ação principal (DS §9): no desktop/tablet fica no Header; no mobile,
 * logo abaixo dele, no topo do conteúdo. NÃO renderize outro <h1> na página.
 */
import * as React from 'react'

export interface Breadcrumb {
  label: string
  to?: string
}

export interface PageMeta {
  title?: string
  description?: React.ReactNode
  actions?: React.ReactNode
  breadcrumbs?: Breadcrumb[]
}

interface PageMetaContextValue {
  meta: PageMeta
  setMeta: (m: PageMeta) => void
}

const PageMetaContext = React.createContext<PageMetaContextValue>({ meta: {}, setMeta: () => undefined })

export function PageMetaProvider({ children }: { children: React.ReactNode }) {
  const [meta, setMeta] = React.useState<PageMeta>({})
  const value = React.useMemo(() => ({ meta, setMeta }), [meta])
  return <PageMetaContext.Provider value={value}>{children}</PageMetaContext.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components
export function usePageMetaValue() {
  return React.useContext(PageMetaContext).meta
}

/** Declara título/descrição/ações da página atual. Não renderiza nada no lugar. */
export function PageHeader({ title, description, actions, breadcrumbs }: PageMeta) {
  const { setMeta } = React.useContext(PageMetaContext)
  React.useLayoutEffect(() => {
    setMeta({ title, description, actions, breadcrumbs })
  }, [setMeta, title, description, actions, breadcrumbs])
  React.useLayoutEffect(() => () => setMeta({}), [setMeta])
  return null
}
