import { describe, expect, it, vi } from 'vitest'
import * as React from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from './dialog'
import { Sheet, SheetContent, SheetDescription, SheetTitle, SheetTrigger } from './sheet'
import { Button } from './button'

function DialogFixture() {
  return (
    <>
      <button type="button">Fora antes</button>
      <Dialog>
        <DialogTrigger asChild>
          <Button>Abrir modal</Button>
        </DialogTrigger>
        <DialogContent>
          <DialogTitle>Editar tela</DialogTitle>
          <DialogDescription>Descrição</DialogDescription>
          <input aria-label="Nome" />
          <Button>Salvar</Button>
        </DialogContent>
      </Dialog>
      <button type="button">Fora depois</button>
    </>
  )
}

describe('Dialog (Modal)', () => {
  it('prende o foco dentro do modal, fecha com Esc e devolve o foco ao gatilho', async () => {
    const user = userEvent.setup()
    render(<DialogFixture />)
    const trigger = screen.getByRole('button', { name: 'Abrir modal' })
    await user.click(trigger)
    const dialog = await screen.findByRole('dialog', { name: 'Editar tela' })
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true))

    // Tab várias vezes: o foco nunca sai do diálogo (focus trap)
    for (let i = 0; i < 6; i++) {
      await user.tab()
      expect(dialog.contains(document.activeElement)).toBe(true)
    }
    await user.tab({ shift: true })
    expect(dialog.contains(document.activeElement)).toBe(true)

    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()
  })

  it('tem botão de fechar com nome acessível', async () => {
    const user = userEvent.setup()
    render(<DialogFixture />)
    await user.click(screen.getByRole('button', { name: 'Abrir modal' }))
    expect(await screen.findByRole('button', { name: 'Fechar' })).toBeInTheDocument()
  })
})

function StateDialogFixture({ onCloseAutoFocus, removeOpener = false }: { onCloseAutoFocus?: (e: Event) => void; removeOpener?: boolean }) {
  const [open, setOpen] = React.useState(false)
  const [opener, setOpener] = React.useState(true)
  return (
    <>
      {opener ? (
        <Button
          onClick={() => {
            setOpen(true)
            if (removeOpener) setOpener(false)
          }}
        >
          Abrir por estado
        </Button>
      ) : null}
      <button type="button">Outro</button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent onCloseAutoFocus={onCloseAutoFocus}>
          <DialogTitle>Cancelar campanha</DialogTitle>
          <DialogDescription>Descrição</DialogDescription>
          <input aria-label="Motivo" />
        </DialogContent>
      </Dialog>
    </>
  )
}

describe('Dialog aberto por estado (sem DialogTrigger)', () => {
  it('Esc fecha e o foco volta ao botão que abriu (não cai no body)', async () => {
    const user = userEvent.setup()
    render(<StateDialogFixture />)
    const opener = screen.getByRole('button', { name: 'Abrir por estado' })
    opener.focus()
    await user.keyboard('{Enter}')
    const dialog = await screen.findByRole('dialog')
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true))
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(opener).toHaveFocus()
  })

  it('o handler onCloseAutoFocus do chamador continua sendo chamado (e pode assumir o foco)', async () => {
    const user = userEvent.setup()
    const handler = vi.fn((e: Event) => {
      e.preventDefault()
      screen.getByRole('button', { name: 'Outro' }).focus()
    })
    render(<StateDialogFixture onCloseAutoFocus={handler} />)
    screen.getByRole('button', { name: 'Abrir por estado' }).focus()
    await user.keyboard('{Enter}')
    await screen.findByRole('dialog')
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(handler).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: 'Outro' })).toHaveFocus()
  })

  it('gatilho removido do DOM enquanto aberto: fecha sem erro', async () => {
    const user = userEvent.setup()
    render(<StateDialogFixture removeOpener />)
    screen.getByRole('button', { name: 'Abrir por estado' }).focus()
    await user.keyboard('{Enter}')
    await screen.findByRole('dialog')
    expect(screen.queryByRole('button', { name: 'Abrir por estado' })).not.toBeInTheDocument()
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })
})

describe('Sheet (Drawer / gaveta)', () => {
  it('Esc fecha e o foco volta ao botão que abriu', async () => {
    const user = userEvent.setup()
    render(
      <Sheet>
        <SheetTrigger asChild>
          <Button>Abrir menu</Button>
        </SheetTrigger>
        <SheetContent side="left" closeLabel="Fechar menu">
          <SheetTitle>Menu</SheetTitle>
          <SheetDescription>Navegação</SheetDescription>
          <a href="#x">Telas</a>
        </SheetContent>
      </Sheet>,
    )
    const trigger = screen.getByRole('button', { name: 'Abrir menu' })
    await user.click(trigger)
    const drawer = await screen.findByRole('dialog', { name: 'Menu' })
    await waitFor(() => expect(drawer.contains(document.activeElement)).toBe(true))
    expect(screen.getByRole('button', { name: 'Fechar menu' })).toBeInTheDocument()
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()
  })

  it('aberta por estado (sem SheetTrigger): Esc devolve o foco ao botão que abriu, não ao <body>', async () => {
    const user = userEvent.setup()
    function StateSheet() {
      const [open, setOpen] = React.useState(false)
      return (
        <>
          <Button onClick={() => setOpen(true)}>Abrir linha</Button>
          <Sheet open={open} onOpenChange={setOpen}>
            <SheetContent>
              <SheetTitle>Detalhe</SheetTitle>
              <SheetDescription>Info</SheetDescription>
            </SheetContent>
          </Sheet>
        </>
      )
    }
    render(<StateSheet />)
    const opener = screen.getByRole('button', { name: 'Abrir linha' })
    await user.click(opener)
    await screen.findByRole('dialog', { name: 'Detalhe' })
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(opener).toHaveFocus()
  })

  it('o botão de fechar visível também devolve o foco', async () => {
    const user = userEvent.setup()
    render(
      <Sheet>
        <SheetTrigger asChild>
          <Button>Detalhes</Button>
        </SheetTrigger>
        <SheetContent>
          <SheetTitle>Detalhe da tela</SheetTitle>
          <SheetDescription>Info</SheetDescription>
        </SheetContent>
      </Sheet>,
    )
    const trigger = screen.getByRole('button', { name: 'Detalhes' })
    await user.click(trigger)
    await user.click(await screen.findByRole('button', { name: 'Fechar' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()
  })
})
