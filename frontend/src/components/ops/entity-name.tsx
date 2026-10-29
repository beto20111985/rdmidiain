/**
 * Nome de entidades a partir do id quando a resposta da API não traz o nome (ex.: históricos com
 * `actor_user_id`). Listas de manutenção/instalação/comodato já trazem os nomes do servidor.
 * Consulta o detalhe com cache de 5 min (deduplicado pelo TanStack Query). Sem permissão/sem
 * resposta → "—" (nunca um nome inventado).
 */
import { Link } from 'react-router-dom'
import { EMPTY_VALUE } from '@/lib/format'
import { useAuthStore } from '@/stores/authStore'
import { useCreativeLookup, useEstablishmentLookup, useScreenLookup, useUserLookup } from '@/pages/ops/api/hooks'

/**
 * `name`/`code` informados pela API (mesmo `null`) dispensam a consulta por id — quando a lista do
 * backend já resolve o nome, nunca há chamada extra; `undefined` = resolver pelo detalhe.
 */
export function EstablishmentName({ id, name, link = true }: { id: string | null | undefined; name?: string | null; link?: boolean }) {
  const q = useEstablishmentLookup(name !== undefined ? null : id)
  const label = name ?? q.data?.name
  if (!id) return <span className="text-text-muted">{EMPTY_VALUE}</span>
  if (!label) return <span className="text-text-muted">{q.isLoading ? 'Carregando…' : EMPTY_VALUE}</span>
  return link ? (
    <Link to={`/estabelecimentos/${id}`} className="font-semibold text-link underline-offset-4 hover:underline">
      {label}
    </Link>
  ) : (
    <span>{label}</span>
  )
}

export function ScreenCode({
  id,
  code,
  link = true,
  basePath = '/telas',
}: {
  id: string | null | undefined
  code?: string | null
  link?: boolean
  basePath?: string
}) {
  const q = useScreenLookup(code !== undefined ? null : id)
  const label = code ?? q.data?.friendly_id
  if (!id) return <span className="text-text-muted">{EMPTY_VALUE}</span>
  if (!label) return <span className="text-text-muted">{q.isLoading ? 'Carregando…' : EMPTY_VALUE}</span>
  return link ? (
    <Link to={`${basePath}/${id}`} className="font-semibold tabular-nums text-link underline-offset-4 hover:underline">
      {label}
    </Link>
  ) : (
    <span className="tabular-nums">{label}</span>
  )
}

export function UserName({ id, name }: { id: string | null | undefined; name?: string | null }) {
  const me = useAuthStore((s) => s.me)
  const isMe = !!id && me?.user.id === id
  // o próprio usuário sempre se reconhece, mesmo sem users.view (sem consulta extra)
  const q = useUserLookup(name !== undefined || isMe ? null : id)
  if (!id) return <span className="text-text-muted">{EMPTY_VALUE}</span>
  if (isMe && name === undefined) return <span>{me?.user.name ? `${me.user.name} (você)` : 'Você'}</span>
  const label = name ?? q.data?.name
  return label ? <span>{label}</span> : <span className="text-text-muted">{q.isLoading ? 'Carregando…' : EMPTY_VALUE}</span>
}

/** Peça (criativo) pelo id: "RD-XXXXXX — nome". Sem permissão/sem resposta → "—". */
export function CreativeLabel({ id }: { id: string | null | undefined }) {
  const q = useCreativeLookup(id)
  if (!id) return <span className="text-text-muted">{EMPTY_VALUE}</span>
  if (!q.data) return <span className="text-text-muted">{q.isLoading ? 'Carregando…' : EMPTY_VALUE}</span>
  return (
    <span className="break-words">
      <span className="font-mono">{q.data.code}</span> — {q.data.name}
    </span>
  )
}
