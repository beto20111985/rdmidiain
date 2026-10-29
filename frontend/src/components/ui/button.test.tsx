import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Button } from './button'

describe('Button', () => {
  it('em loading fica desabilitado mesmo com disabled={false} e não dispara clique (A3: loading clicável)', async () => {
    const onClick = vi.fn()
    render(
      <Button loading disabled={false} onClick={onClick}>
        Salvar
      </Button>,
    )
    const btn = screen.getByRole('button')
    expect(btn).toBeDisabled()
    expect(btn).toHaveAttribute('aria-busy', 'true')
    await userEvent.click(btn)
    expect(onClick).not.toHaveBeenCalled()
  })

  it('anuncia o progresso e mantém o conteúdo (largura preservada)', () => {
    render(
      <Button loading loadingText="Salvando…">
        Salvar
      </Button>,
    )
    expect(screen.getByRole('status')).toHaveTextContent('Salvando…')
    // O rótulo original continua no DOM (invisível) para manter a largura.
    expect(screen.getByText('Salvar')).toBeInTheDocument()
  })

  it('fora do loading dispara o clique e tem altura mínima de 44 px (min-h-11)', async () => {
    const onClick = vi.fn()
    render(<Button onClick={onClick}>Criar campanha</Button>)
    const btn = screen.getByRole('button', { name: 'Criar campanha' })
    expect(btn.className).toContain('min-h-11')
    expect(btn).toHaveAttribute('type', 'button')
    await userEvent.click(btn)
    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('tem as quatro variantes do DS', () => {
    render(
      <>
        <Button>Primário</Button>
        <Button variant="secondary">Secundário</Button>
        <Button variant="tertiary">Terciário</Button>
        <Button variant="destructive">Excluir</Button>
      </>,
    )
    expect(screen.getByText('Primário').className).toContain('bg-action')
    expect(screen.getByText('Secundário').className).toContain('border-control-border')
    expect(screen.getByText('Terciário').className).toContain('text-link')
    expect(screen.getByText('Excluir').className).toContain('text-danger')
  })
})
