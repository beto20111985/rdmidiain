import { describe, expect, it, vi } from 'vitest'
import * as React from 'react'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { KpiCard } from './kpi-card'
import { DataTable, type DataTableColumn } from './data-table'
import { ConfirmDialog } from './confirm-dialog'
import { StatusBadge } from './status-badge'
import { useTableState } from '@/hooks/useTableState'
import { renderWithProviders } from '@/test/utils'
import { Field, FieldError, FieldLabel, FieldDescription } from '@/components/ui/field'
import { Input } from '@/components/ui/input'

describe('KpiCard (DS §10)', () => {
  it('sem dado mostra "—" (nunca zero inventado)', () => {
    renderWithProviders(<KpiCard label="Receita" value={null} />)
    expect(screen.getByText('—')).toBeInTheDocument()
    expect(screen.getByText('Sem dados')).toBeInTheDocument()
  })

  it('base anterior ausente → "Sem base de comparação"', () => {
    renderWithProviders(<KpiCard label="Anunciantes" value={3} comparison={{ deltaPct: null, periodLabel: 'vs. mês anterior' }} />)
    expect(screen.getByText('Sem base de comparação')).toBeInTheDocument()
  })

  it('queda numa métrica "menor é melhor" é anunciada como melhora, com o período', () => {
    renderWithProviders(
      <KpiCard label="Falhas" value={4} comparison={{ deltaPct: -20, periodLabel: 'vs. 7 dias anteriores', polarity: 'lower-is-better' }} />,
    )
    expect(screen.getByText('(melhora)')).toBeInTheDocument()
    expect(screen.getByText('vs. 7 dias anteriores')).toBeInTheDocument()
  })
})

describe('StatusBadge', () => {
  it('status sempre com texto (não só cor)', () => {
    renderWithProviders(<StatusBadge status="offline" />)
    expect(screen.getByText('Offline')).toBeInTheDocument()
  })
})

describe('Field (label persistente + erro associado)', () => {
  it('liga label, descrição e erro ao controle via id/aria-describedby/aria-invalid', () => {
    renderWithProviders(
      <Field invalid>
        <FieldLabel required>E-mail</FieldLabel>
        <Input />
        <FieldDescription>Usado para entrar.</FieldDescription>
        <FieldError>Informe um e-mail válido.</FieldError>
      </Field>,
    )
    const input = screen.getByRole('textbox', { name: /E-mail/ })
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(input).toHaveAccessibleDescription('Usado para entrar. Informe um e-mail válido.')
  })
})

interface Row {
  id: string
  name: string
  plays: number
}

function TableHarness({ rows }: { rows: Row[] }) {
  const table = useTableState({ defaultPageSize: 10 })
  const columns: DataTableColumn<Row>[] = [
    { id: 'name', header: 'Nome', sortField: 'name', cell: (r) => r.name },
    { id: 'plays', header: 'Exibições', numeric: true, sortField: 'plays', cell: (r) => r.plays },
  ]
  const page = rows.slice((table.state.page - 1) * 10, table.state.page * 10)
  return (
    <>
      <p data-testid="params">{JSON.stringify(table.apiParams)}</p>
      <DataTable
        caption="Telas"
        columns={columns}
        data={{ items: page, total: rows.length, page: table.state.page, page_size: 10 }}
        getRowId={(r) => r.id}
        controller={table}
        empty={{ title: 'Adicione sua primeira tela para começar.' }}
      />
    </>
  )
}

describe('DataTable', () => {
  const rows = Array.from({ length: 23 }, (_, i) => ({ id: String(i), name: `Tela ${i}`, plays: i }))

  it('ordenação anunciada (aria-sort) e refletida nos parâmetros da API', async () => {
    const user = userEvent.setup()
    renderWithProviders(<TableHarness rows={rows} />)
    const header = screen.getByRole('columnheader', { name: /Nome/ })
    expect(header).toHaveAttribute('aria-sort', 'none')
    await user.click(within(header).getByRole('button'))
    expect(header).toHaveAttribute('aria-sort', 'ascending')
    expect(screen.getByTestId('params')).toHaveTextContent('"sort":"name"')
    await user.click(within(header).getByRole('button'))
    expect(header).toHaveAttribute('aria-sort', 'descending')
    expect(screen.getByTestId('params')).toHaveTextContent('"sort":"-name"')
    expect(screen.getByText(/Ordenado por Nome, decrescente/)).toBeInTheDocument()
  })

  it('paginação do contrato (page/page_size/total) com botões de 44 px nomeados', async () => {
    const user = userEvent.setup()
    renderWithProviders(<TableHarness rows={rows} />)
    expect(screen.getByText('Página 1 de 3')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Página anterior' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Próxima página' }))
    expect(screen.getByText('Página 2 de 3')).toBeInTheDocument()
    expect(screen.getByTestId('params')).toHaveTextContent('"page":2')
    expect(screen.getByText('Tela 10')).toBeInTheDocument()
  })

  it('vazio mostra explicação do DS', () => {
    renderWithProviders(<TableHarness rows={[]} />)
    expect(screen.getByText('Adicione sua primeira tela para começar.')).toBeInTheDocument()
  })
})

describe('ConfirmDialog', () => {
  it('exclusão exige digitar o nome antes de habilitar a confirmação', async () => {
    const user = userEvent.setup()
    const onConfirm = vi.fn()
    renderWithProviders(
      <ConfirmDialog
        open
        onOpenChange={() => undefined}
        destructive
        title="Excluir a campanha Verão?"
        confirmLabel="Excluir campanha"
        requireText="Verão"
        onConfirm={onConfirm}
      />,
    )
    const confirm = screen.getByRole('button', { name: 'Excluir campanha' })
    expect(confirm).toBeDisabled()
    await user.type(screen.getByRole('textbox'), 'Verã')
    expect(confirm).toBeDisabled()
    await user.type(screen.getByRole('textbox'), 'o')
    expect(confirm).toBeEnabled()
    await user.click(confirm)
    expect(onConfirm).toHaveBeenCalledTimes(1)
  })

  it('texto de apoio coerente com a ação: publicar NÃO fala em exclusão; destrutiva sim; prop explícita prevalece', () => {
    const base = { open: true, onOpenChange: () => undefined, onConfirm: () => undefined, requireText: 'v2' }
    const { unmount } = renderWithProviders(<ConfirmDialog {...base} title="Publicar a versão 2?" confirmLabel="Publicar" />)
    expect(screen.queryByText(/exclus|remover/i)).toBeNull()
    expect(screen.getByText('Esta confirmação evita que a ação seja feita por engano.')).toBeInTheDocument()
    unmount()
    const r2 = renderWithProviders(<ConfirmDialog {...base} destructive title="Excluir?" confirmLabel="Excluir" />)
    expect(screen.getByText('Esta confirmação evita remover ou desfazer algo por engano.')).toBeInTheDocument()
    r2.unmount()
    renderWithProviders(<ConfirmDialog {...base} title="Publicar?" confirmLabel="Publicar" confirmHint="Digitar a versão evita publicar o texto errado." />)
    expect(screen.getByText('Digitar a versão evita publicar o texto errado.')).toBeInTheDocument()
  })

  it('aberto por estado (sem gatilho Radix): ao fechar, o foco volta ao botão que abriu, não ao <body>', async () => {
    const user = userEvent.setup()
    function Fixture() {
      const [open, setOpen] = React.useState(false)
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>
            Publicar versão
          </button>
          <ConfirmDialog open={open} onOpenChange={setOpen} title="Publicar?" confirmLabel="Publicar" onConfirm={() => undefined} />
        </>
      )
    }
    renderWithProviders(<Fixture />)
    const opener = screen.getByRole('button', { name: 'Publicar versão' })
    await user.click(opener)
    const dialog = await screen.findByRole('alertdialog')
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true))
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(opener).toHaveFocus()
    // e também após confirmar
    await user.click(opener)
    await user.click(await screen.findByRole('button', { name: 'Publicar' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(opener).toHaveFocus()
  })

  it('erro da ação fica dentro do diálogo', async () => {
    const user = userEvent.setup()
    renderWithProviders(
      <ConfirmDialog
        open
        onOpenChange={() => undefined}
        title="Encerrar sessão?"
        confirmLabel="Encerrar"
        onConfirm={() => Promise.reject(new Error('Falhou no servidor.'))}
      />,
    )
    await user.click(screen.getByRole('button', { name: 'Encerrar' }))
    expect(await screen.findByText('Falhou no servidor.')).toBeInTheDocument()
    expect(screen.getByRole('alertdialog')).toBeInTheDocument()
  })
})
