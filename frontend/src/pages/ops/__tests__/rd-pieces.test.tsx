import { afterEach, describe, expect, it, vi } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from '@/test/utils'
import { apiError, callsTo, routeFetch, setMe } from '@/test/opsTestUtils'
import { RdPiecesPanel } from '../content/RdPiecesPanel'
import { suggestDuration } from '../content/rdPieceRules'
import type { MediaLite } from '../api/types'

// Fluxos digitados tecla a tecla: limite explícito (CPU do container compartilhada).
vi.setConfig({ testTimeout: 30_000 })

afterEach(() => vi.unstubAllGlobals())

const VIDEO: MediaLite = {
  id: 'm1',
  org_id: 'o9',
  name: 'Vinheta da padaria',
  kind: 'VIDEO',
  status: 'READY',
  source: { duration_ms: 59_800 },
  output: { duration_ms: 59_500, orientation: 'HORIZONTAL' },
}

const PENDING_PIECE = {
  id: 'c1',
  code: 'RD-ABC234',
  org_id: 'o9',
  name: 'Cardápio do dia',
  kind: 'INTERNAL',
  duration_seconds: 30,
  orientation: 'HORIZONTAL',
  status: 'RASCUNHO',
  technical_status: 'VALIDO',
  created_at: '2026-10-08T10:00:00Z',
}

const page = <T,>(items: T[]) => ({ items, total: items.length, page: 1, page_size: 25 })

describe('Peças da RD: cadastro e revisão de conteúdo (POST /creatives/{id}/review)', () => {
  it('cadastra peça INTERNAL a partir de mídia pronta, com a org da mídia e duração sugerida pelo vídeo', async () => {
    setMe(['creatives.view', 'creatives.manage', 'campaigns.approve'])
    const user = userEvent.setup()
    const f = routeFetch([
      { path: '/creatives', reply: () => page([]) },
      { path: '/media', reply: () => page([VIDEO]) },
      { method: 'POST', path: '/creatives', reply: () => ({ ...PENDING_PIECE, id: 'c9', code: 'RD-NEW234', duration_seconds: 60 }) },
    ])
    renderWithProviders(<RdPiecesPanel />)
    expect(await screen.findByText('Nenhuma peça aguardando revisão.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Nova peça' }))
    // envio sem preencher: erros inline, nada é enviado
    await user.click(screen.getByRole('button', { name: 'Cadastrar peça' }))
    expect(screen.getByText('Informe o nome da peça (mín. 2 caracteres).')).toBeInTheDocument()
    expect(screen.getByText('Escolha uma mídia pronta da biblioteca.')).toBeInTheDocument()
    expect(callsTo(f, '/creatives', 'POST')).toHaveLength(0)

    await user.type(screen.getByLabelText(/Nome da peça/), 'Vinheta institucional')
    await waitFor(() => expect(callsTo(f, '/media')).not.toHaveLength(0))
    const media = new URL(String(callsTo(f, '/media')[0][0]), 'http://x')
    expect(media.searchParams.get('status')).toBe('READY')
    const pickerGroup = screen.getByRole('group', { name: /Mídia da biblioteca/ })
    await user.click(within(pickerGroup).getByRole('combobox'))
    await user.click(await screen.findByRole('option', { name: /Vinheta da padaria \(vídeo, 59,5 s\)/ }))
    expect(screen.getByText(/O vídeo tem 59,5 s/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Cadastrar peça' }))
    await waitFor(() => expect(callsTo(f, '/creatives', 'POST')).toHaveLength(1))
    expect(JSON.parse(String(callsTo(f, '/creatives', 'POST')[0][1]!.body))).toEqual({
      org_id: 'o9',
      name: 'Vinheta institucional',
      media_file_id: 'm1',
      duration_seconds: 60,
      kind: 'INTERNAL',
    })
  })

  it('aprova pela rota de revisão com confirmação', async () => {
    setMe(['creatives.view', 'campaigns.approve'])
    const user = userEvent.setup()
    const f = routeFetch([
      { path: '/creatives', reply: () => page([PENDING_PIECE]) },
      { method: 'POST', path: '/creatives/c1/review', reply: () => ({ ...PENDING_PIECE, status: 'APROVADO' }) },
    ])
    renderWithProviders(<RdPiecesPanel />)
    const list = new URL(String((await waitFor(() => callsTo(f, '/creatives')[0]))[0]), 'http://x')
    expect(list.searchParams.get('kind')).toBe('INTERNAL')
    expect(list.searchParams.get('status')).toBe('RASCUNHO,EM_ANALISE')
    await user.click(await screen.findByRole('button', { name: 'Aprovar a peça RD-ABC234' }))
    const dialog = await screen.findByRole('alertdialog')
    await user.click(within(dialog).getByRole('button', { name: 'Aprovar peça' }))
    await waitFor(() => expect(callsTo(f, '/creatives/c1/review', 'POST')).toHaveLength(1))
    expect(JSON.parse(String(callsTo(f, '/creatives/c1/review', 'POST')[0][1]!.body))).toEqual({ decision: 'APPROVE' })
    // sem permissão de cadastro: não oferece "Nova peça"
    expect(screen.queryByRole('button', { name: 'Nova peça' })).toBeNull()
  })

  it('mídia ainda não pronta: o erro do servidor aparece no diálogo de aprovação', async () => {
    setMe(['creatives.view', 'campaigns.approve'])
    const user = userEvent.setup()
    routeFetch([
      { path: '/creatives', reply: () => page([PENDING_PIECE]) },
      {
        method: 'POST',
        path: '/creatives/c1/review',
        reply: () => apiError(409, 'CREATIVE_NOT_READY', 'A mídia do criativo precisa estar pronta (READY) e com duração válida.'),
      },
    ])
    renderWithProviders(<RdPiecesPanel />)
    await user.click(await screen.findByRole('button', { name: 'Aprovar a peça RD-ABC234' }))
    const dialog = await screen.findByRole('alertdialog')
    await user.click(within(dialog).getByRole('button', { name: 'Aprovar peça' }))
    expect(await within(dialog).findByText(/precisa estar pronta/)).toBeInTheDocument()
  })

  it('rejeitar exige motivo (≥ 10) e envia REJECT com o motivo', async () => {
    setMe(['creatives.view', 'campaigns.approve'])
    const user = userEvent.setup()
    const f = routeFetch([
      { path: '/creatives', reply: () => page([PENDING_PIECE]) },
      { method: 'POST', path: '/creatives/c1/review', reply: () => ({ ...PENDING_PIECE, status: 'REJEITADO' }) },
    ])
    renderWithProviders(<RdPiecesPanel />)
    await user.click(await screen.findByRole('button', { name: 'Rejeitar a peça RD-ABC234' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByLabelText(/Motivo da rejeição/), 'curto')
    await user.click(within(dialog).getByRole('button', { name: 'Rejeitar peça' }))
    expect(callsTo(f, '/creatives/c1/review', 'POST')).toHaveLength(0)
    await user.type(within(dialog).getByLabelText(/Motivo da rejeição/), ' — logotipo antigo da marca')
    await user.click(within(dialog).getByRole('button', { name: 'Rejeitar peça' }))
    await waitFor(() => expect(callsTo(f, '/creatives/c1/review', 'POST')).toHaveLength(1))
    expect(JSON.parse(String(callsTo(f, '/creatives/c1/review', 'POST')[0][1]!.body))).toEqual({
      decision: 'REJECT',
      reason: 'curto — logotipo antigo da marca',
    })
  })

  it('sem campaigns.approve não há ações de revisão', async () => {
    setMe(['creatives.view'])
    routeFetch([{ path: '/creatives', reply: () => page([PENDING_PIECE]) }])
    renderWithProviders(<RdPiecesPanel />)
    expect(await screen.findByText('Cardápio do dia', { exact: false })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Aprovar|Rejeitar/ })).toBeNull()
    expect(screen.getByText(/revisão de conteúdo exige a permissão campaigns.approve/)).toBeInTheDocument()
  })

  it('duração sugerida: duração permitida mais próxima do vídeo; imagem não sugere', () => {
    expect(suggestDuration(VIDEO)).toBe(60)
    expect(suggestDuration({ ...VIDEO, output: { duration_ms: 118_000, orientation: 'HORIZONTAL' } })).toBe(120)
    expect(suggestDuration({ ...VIDEO, kind: 'IMAGE' })).toBeNull()
    expect(suggestDuration(undefined)).toBeNull()
  })
})
