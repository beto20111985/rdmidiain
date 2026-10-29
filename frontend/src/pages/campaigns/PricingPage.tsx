/**
 * Planos e preços (PG-16; RD, `pricing.view` lê e `pricing.manage` altera): tabelas de preço
 * versionadas, planos pré-definidos e personalizados e promoções. Cupons e margem têm páginas
 * próprias no menu. Aba ativa na URL (`?aba=`).
 */
import { useSearchParams } from 'react-router-dom'
import { PageHeader } from '@/components/layout/page-meta'
import { Card } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useCan } from '@/hooks/usePermissions'
import { TablesTab } from './pricing/TablesTab'
import { PlansTab } from './pricing/PlansTab'
import { PromotionsTab } from './pricing/PromotionsTab'

const TABS = [
  ['tabelas', 'Tabelas de preço'],
  ['planos', 'Planos'],
  ['promocoes', 'Promoções'],
] as const

export default function PricingPage() {
  const can = useCan()
  const manage = can('pricing.manage')
  const [sp, setSp] = useSearchParams()
  const tab = TABS.some(([k]) => k === sp.get('aba')) ? (sp.get('aba') as string) : 'tabelas'
  return (
    <>
      <PageHeader title="Tabelas de preço" description="Preços mudam sem deploy: cada versão é imutável depois de ativada." />
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
        <TabsList aria-label="Seções de preços">
          {TABS.map(([k, l]) => (
            <TabsTrigger key={k} value={k}>
              {l}
            </TabsTrigger>
          ))}
        </TabsList>
        <Card className="mt-6">
          <TabsContent value="tabelas" className="mt-0">
            <TablesTab canManage={manage} />
          </TabsContent>
          <TabsContent value="planos" className="mt-0">
            <PlansTab canManage={manage} />
          </TabsContent>
          <TabsContent value="promocoes" className="mt-0">
            <PromotionsTab canManage={manage} />
          </TabsContent>
        </Card>
      </Tabs>
    </>
  )
}
