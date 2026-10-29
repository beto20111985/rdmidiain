import { describe, expect, it, vi } from 'vitest'
import * as React from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Field, FieldLabel } from '@/components/ui/field'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from './select'

/**
 * Regressão do laço do Radix Select controlado dentro de <form> — consolidada na fundação a partir
 * das cópias locais de OPS (pickers/filter-select), CAMP (safe-select), ADM (select-guard) e FIN
 * (safe-select). Com o valor pré-preenchido e as opções chegando depois, o <select> nativo oculto
 * emite '' — repassá-lo apagava a escolha e, com o efeito que restaura o valor, gerava laço síncrono
 * (a suíte travava). Este arquivo FALHA se a proteção de `Select` for removida.
 */
type Status = 'ATIVO' | 'INATIVO'

function Harness({
  options,
  onChange,
  initial = 'org1',
}: {
  options: Array<{ id: string; name: string }>
  onChange: (v: string) => void
  initial?: string
}) {
  const [value, setValue] = React.useState(initial)
  const renders = React.useRef(0)
  renders.current += 1
  // Efeito que "restaura" o valor se ele for apagado — o padrão que travava a suíte (OPS PairingPage, FIN, ADM).
  React.useEffect(() => {
    if (!value) setValue(initial)
  }, [value, initial])
  return (
    <form>
      <Field>
        <FieldLabel>Organização</FieldLabel>
        <Select
          value={value}
          onValueChange={(v) => {
            onChange(v)
            setValue(v)
          }}
        >
          <SelectTrigger>
            <SelectValue placeholder="Selecione" />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              {options.map((o) => (
                <SelectItem key={o.id} value={o.id}>
                  {o.name}
                </SelectItem>
              ))}
            </SelectGroup>
          </SelectContent>
        </Select>
      </Field>
      <output data-testid="value">{value}</output>
      <output data-testid="renders">{renders.current}</output>
    </form>
  )
}

/**
 * Reprodução fiel do mecanismo: o `SelectBubbleInput` do Radix, quando o valor controlado MUDA, grava o
 * valor no <select> nativo e dispara `change`; sem <option> correspondente o nativo lê '' e o Radix chama
 * `onValueChange('')`. O pré-preenchimento chega depois do mount (ex.: GET do registro) e um efeito o
 * restaura quando o valor some — sem a proteção isso vira laço (aqui limitado por contador para falhar
 * de forma limpa em vez de travar o worker).
 */
function PrefillHarness({
  prefill,
  options,
  onChange,
}: {
  prefill: string
  options: Array<{ id: string; name: string }>
  onChange: (v: string) => void
}) {
  const [value, setValue] = React.useState('')
  const restores = React.useRef(0)
  React.useEffect(() => {
    if (prefill && !value && restores.current < 25) {
      restores.current += 1
      setValue(prefill)
    }
  }, [prefill, value])
  return (
    <form>
      <Field>
        <FieldLabel>Responsável</FieldLabel>
        <Select
          value={value}
          onValueChange={(v) => {
            onChange(v)
            setValue(v)
          }}
        >
          <SelectTrigger>
            <SelectValue placeholder="Selecione" />
          </SelectTrigger>
          <SelectContent>
            {options.map((o) => (
              <SelectItem key={o.id} value={o.id}>
                {o.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </Field>
      <output data-testid="value">{value}</output>
      <output data-testid="restores">{restores.current}</output>
    </form>
  )
}

const OPTIONS = [
  { id: 'org1', name: 'Cliente Um' },
  { id: 'org2', name: 'Cliente Dois' },
]

describe('Select da fundação — proteção única contra o laço do Radix em <form>', () => {
  it('valor pré-preenchido sobrevive às opções chegando depois, sem receber "" e sem laço', () => {
    const onChange = vi.fn()
    const { rerender } = render(<Harness options={[]} onChange={onChange} />)
    rerender(<Harness options={OPTIONS} onChange={onChange} />)
    expect(screen.getByTestId('value')).toHaveTextContent('org1')
    expect(onChange).not.toHaveBeenCalledWith('')
    expect(onChange).not.toHaveBeenCalledWith('org1') // valor repetido não é repassado
    expect(Number(screen.getByTestId('renders').textContent)).toBeLessThan(10)
    expect(screen.getByRole('combobox', { name: 'Organização' })).toHaveTextContent('Cliente Um')
  })

  it('pré-preenchimento que chega ANTES das opções: o "" do <select> nativo não apaga o valor (sem laço)', () => {
    const onChange = vi.fn()
    const { rerender } = render(<PrefillHarness prefill="" options={[]} onChange={onChange} />)
    rerender(<PrefillHarness prefill="org1" options={[]} onChange={onChange} />)
    rerender(<PrefillHarness prefill="org1" options={OPTIONS} onChange={onChange} />)
    expect(onChange).not.toHaveBeenCalledWith('')
    expect(onChange).not.toHaveBeenCalled()
    expect(screen.getByTestId('value')).toHaveTextContent('org1')
    expect(screen.getByTestId('restores')).toHaveTextContent('1')
    expect(screen.getByRole('combobox', { name: 'Responsável' })).toHaveTextContent('Cliente Um')
  })

  it('opções que nunca chegam (lista vazia) não apagam o valor nem disparam onValueChange', () => {
    const onChange = vi.fn()
    render(<Harness options={[]} onChange={onChange} />)
    expect(onChange).not.toHaveBeenCalled()
    expect(screen.getByTestId('value')).toHaveTextContent('org1')
  })

  it('escolha real do usuário continua sendo repassada (tipada pela união do valor)', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<Harness options={OPTIONS} onChange={onChange} />)
    await user.click(screen.getByRole('combobox', { name: 'Organização' }))
    await user.click(await screen.findByRole('option', { name: 'Cliente Dois' }))
    expect(onChange).toHaveBeenCalledWith('org2')
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('value')).toHaveTextContent('org2')
  })

  it('genérico: onValueChange recebe o tipo do valor (sem cast nos usos)', () => {
    function Typed() {
      const [status, setStatus] = React.useState<Status>('ATIVO')
      return (
        <Select value={status} onValueChange={setStatus}>
          <SelectTrigger aria-label="Status">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="ATIVO">Ativo</SelectItem>
            <SelectItem value="INATIVO">Inativo</SelectItem>
          </SelectContent>
        </Select>
      )
    }
    render(<Typed />)
    expect(screen.getByRole('combobox', { name: 'Status' })).toHaveTextContent('Ativo')
  })
})
