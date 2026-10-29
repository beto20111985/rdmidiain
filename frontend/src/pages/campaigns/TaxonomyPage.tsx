/**
 * Taxonomia e conflitos (PG-15; RD, `taxonomy.manage`/`conflicts.override`): árvores de
 * estabelecimentos e de segmentos, política de conteúdo, marcas, produtos, concorrentes,
 * regras explícitas da matriz, exceções do estabelecimento, overrides e simulador.
 * A aba ativa fica na URL (`?aba=`) para links diretos e voltar sem perder o contexto.
 */
import { useSearchParams } from 'react-router-dom'
import { PageHeader } from '@/components/layout/page-meta'
import { Card } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useCan } from '@/hooks/usePermissions'
import { TreeTab } from './taxonomy/TreeTab'
import { BrandsTab, CompetitorsTab, PoliciesTab, ProductsTab } from './taxonomy/CatalogTabs'
import { ExceptionsTab, OverridesTab, RulesTab, SimulatorTab } from './taxonomy/MatrixTabs'

const TABS = [
  ['estabelecimentos', 'Categorias de estabelecimentos'],
  ['segmentos', 'Segmentos'],
  ['politica', 'Política de conteúdo'],
  ['marcas', 'Marcas'],
  ['produtos', 'Produtos'],
  ['concorrentes', 'Concorrentes'],
  ['regras', 'Regras da matriz'],
  ['excecoes', 'Exceções'],
  ['overrides', 'Overrides'],
  ['simulador', 'Simulador'],
] as const

export default function TaxonomyPage() {
  const can = useCan()
  const [sp, setSp] = useSearchParams()
  const tab = TABS.some(([k]) => k === sp.get('aba')) ? (sp.get('aba') as string) : 'estabelecimentos'
  const manage = can('taxonomy.manage')
  return (
    <>
      <PageHeader title="Taxonomia e conflitos" description="Classificação usada para evitar concorrentes e conteúdo proibido nas telas." />
      <Tabs
        value={tab}
        onValueChange={(v) =>
          setSp(
            (prev) => {
              const n = new URLSearchParams(prev)
              n.set('aba', v)
              return n
            },
            { replace: true },
          )
        }
      >
        <TabsList aria-label="Seções da taxonomia">
          {TABS.map(([k, label]) => (
            <TabsTrigger key={k} value={k}>
              {label}
            </TabsTrigger>
          ))}
        </TabsList>
        <Card className="mt-6">
          <TabsContent value="estabelecimentos" className="mt-0">
            <TreeTab variant="nodes" canManage={manage} />
          </TabsContent>
          <TabsContent value="segmentos" className="mt-0">
            <TreeTab variant="segments" canManage={manage} />
          </TabsContent>
          <TabsContent value="politica" className="mt-0">
            <PoliciesTab canManage={manage} />
          </TabsContent>
          <TabsContent value="marcas" className="mt-0">
            <BrandsTab canManage={manage} />
          </TabsContent>
          <TabsContent value="produtos" className="mt-0">
            <ProductsTab canManage={manage} />
          </TabsContent>
          <TabsContent value="concorrentes" className="mt-0">
            <CompetitorsTab canManage={manage} />
          </TabsContent>
          <TabsContent value="regras" className="mt-0">
            <RulesTab />
          </TabsContent>
          <TabsContent value="excecoes" className="mt-0">
            <ExceptionsTab />
          </TabsContent>
          <TabsContent value="overrides" className="mt-0">
            <OverridesTab canOverride={can('conflicts.override')} />
          </TabsContent>
          <TabsContent value="simulador" className="mt-0">
            <SimulatorTab />
          </TabsContent>
        </Card>
      </Tabs>
    </>
  )
}
