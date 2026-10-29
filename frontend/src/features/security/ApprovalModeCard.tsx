/**
 * Cartão "Aprovação financeira: Única | Dupla" (SPEC AÇÃO 003 §11.7). SEM switch: o modo só muda pelo
 * diálogo guiado (consequências, motivo e fatores), que cria um pedido de aprovação.
 * Na ÚNICA o cartão não fala de segundo Master (a explicação do outro modo só aparece no diálogo,
 * aberto por uma ação deliberada). Histórico, divergência de ENV e integridade: só Master / auditoria.
 */
import * as React from 'react'
import { Link } from 'react-router-dom'
import { AlertTriangle, ArrowRightLeft, ChevronDown, History, ShieldAlert } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Separator } from '@/components/ui/separator'
import { cn } from '@/lib/utils'
import { formatDateTime } from '@/lib/format'
import { qk } from '@/lib/queryKeys'
import { APPROVAL_MODE_LABEL } from '@/components/finance/approval-utils'
import { securityApi } from './api'
import { ApprovalModeChangeDialog } from './ApprovalModeChangeDialog'
import type { ApprovalModeHistoryItem, ApprovalModeInfo } from './types'

const SOURCE_LABEL: Record<string, string> = {
  BOOTSTRAP_ENV: 'primeira inicialização',
  LEGACY_DUAL_ADOPTED: 'instalação existente (dupla aprovação adotada)',
  APPROVAL_REQUEST: 'pedido de aprovação',
}

const MODE_EXPLANATION = {
  SINGLE:
    'Aprovação única: o Administrador Master autoriza cada operação financeira com código do autenticador (TOTP), senha financeira e código enviado por e-mail. Toda autorização fica registrada na auditoria.',
  DUAL: 'Dupla aprovação: toda operação financeira sensível é solicitada por um Administrador Master e aprovada pelo outro, com autenticador, senha financeira e código por e-mail. O banco de dados impede que a mesma pessoa solicite e aprove.',
} as const

function ModePills({ mode }: { mode: 'SINGLE' | 'DUAL' }) {
  return (
    <div role="group" aria-label={`Modo atual: ${APPROVAL_MODE_LABEL[mode]}`} className="inline-flex overflow-hidden rounded-lg border border-control-border text-sm font-semibold">
      {(['SINGLE', 'DUAL'] as const).map((m) => (
        <span
          key={m}
          aria-current={m === mode ? 'true' : undefined}
          className={cn('inline-flex min-h-9 items-center px-4', m === mode ? 'bg-action text-action-foreground' : 'text-text-muted')}
        >
          {APPROVAL_MODE_LABEL[m]}
          {m === mode ? <span className="sr-only"> (modo atual)</span> : null}
        </span>
      ))}
    </div>
  )
}

function historyLine(h: ApprovalModeHistoryItem): string {
  if (h.kind === 'INITIALIZED') return `Definido como ${APPROVAL_MODE_LABEL[h.to_mode].toLowerCase()} (${SOURCE_LABEL[h.source] ?? h.source}).`
  const who = [h.requested_by_name ? `solicitado por ${h.requested_by_name}` : null, h.authorized_by_name ? `${h.kind === 'DUAL_TO_SINGLE' ? 'aprovado' : 'autorizado'} por ${h.authorized_by_name}` : null]
    .filter(Boolean)
    .join(' · ')
  return `${h.from_mode ? APPROVAL_MODE_LABEL[h.from_mode] : '—'} → ${APPROVAL_MODE_LABEL[h.to_mode]}${who ? ` (${who})` : ''}.`
}

/**
 * Resumo da VERSÃO VIGENTE (não da inicialização): data e tipo da última transição, ou — se a versão
 * atual é a inicial — data e origem da inicialização. Sem histórico (quem não é Master/auditoria) e
 * com versão > 1, não mostra data alguma (a da v1 seria enganosa).
 */
function currentVersionSummary(info: ApprovalModeInfo): string {
  const current = (info.history ?? []).find((h) => h.mode_version === info.mode_version)
  if (current) {
    if (current.kind === 'INITIALIZED') return `definida em ${formatDateTime(current.created_at)} (${SOURCE_LABEL[current.source] ?? current.source})`
    const change = current.from_mode ? `${APPROVAL_MODE_LABEL[current.from_mode]} → ${APPROVAL_MODE_LABEL[current.to_mode]}` : APPROVAL_MODE_LABEL[current.to_mode]
    return `em vigor desde ${formatDateTime(current.created_at)} · ${change}`
  }
  if (info.mode_version === 1 && info.initialized_at) {
    return `definida em ${formatDateTime(info.initialized_at)}${info.initialized_source ? ` (${SOURCE_LABEL[info.initialized_source] ?? info.initialized_source})` : ''}`
  }
  return ''
}

function IntegrityNotice() {
  const q = useQuery({ queryKey: qk.sub('security', 'integrity'), queryFn: securityApi.integrity })
  return (
    <Alert variant="danger" role="alert">
      <ShieldAlert aria-hidden="true" />
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <AlertTitle>Verificação de integridade da segurança falhou</AlertTitle>
        <AlertDescription className="text-text">
          Operações financeiras ficam suspensas até a verificação voltar ao normal. Contate o responsável técnico.
          {q.data?.problems?.length ? (
            <ul className="mb-0 mt-2 list-disc pl-5">
              {q.data.problems.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          ) : null}
        </AlertDescription>
      </div>
    </Alert>
  )
}

export function ApprovalModeCard({ info, isMaster }: { info: ApprovalModeInfo; isMaster: boolean }) {
  const [dialogOpen, setDialogOpen] = React.useState(false)
  const target = info.mode === 'SINGLE' ? 'DUAL' : 'SINGLE'
  const pending = info.pending_change_request
  // Voltar à aprovação única exige o outro Master ativo (não há aprovador sem ele).
  const blockedByReadiness = info.mode === 'DUAL' && !info.dual_approval_ready

  return (
    <Card aria-labelledby="approval-mode-title">
      <CardHeader>
        <div className="flex flex-col gap-2">
          <CardTitle id="approval-mode-title">Aprovação financeira</CardTitle>
          <CardDescription>
            Versão {info.mode_version}
            {currentVersionSummary(info) ? ` · ${currentVersionSummary(info)}` : ''}
          </CardDescription>
        </div>
        <ModePills mode={info.mode} />
      </CardHeader>

      <p className="m-0 max-w-prose text-sm text-text">{MODE_EXPLANATION[info.mode]}</p>

      {info.integrity && !info.integrity.ok ? <IntegrityNotice /> : null}

      {info.env?.divergence ? (
        <Alert variant="warning">
          <AlertTriangle aria-hidden="true" />
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <AlertTitle>A configuração do ambiente difere do modo em vigor</AlertTitle>
            <AlertDescription className="text-text">
              A variável de inicialização (RD_BOOTSTRAP_DUAL_MASTER_APPROVAL) só vale na primeira inicialização e foi ignorada. O modo em vigor é o
              registrado e selado no banco de dados; ele só muda pelo fluxo de aprovação abaixo.
            </AlertDescription>
          </div>
        </Alert>
      ) : null}
      {info.env?.unsupported_var_present ? (
        <Alert variant="warning">
          <AlertTriangle aria-hidden="true" />
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <AlertTitle>Variável de ambiente não utilizada</AlertTitle>
            <AlertDescription className="text-text">
              RD_DUAL_MASTER_APPROVAL não é lida pelo sistema. O nome correto é RD_BOOTSTRAP_DUAL_MASTER_APPROVAL e ela só vale na primeira
              inicialização — trocar a variável não muda o modo.
            </AlertDescription>
          </div>
        </Alert>
      ) : null}

      {pending ? (
        <Alert variant="info" data-testid="pending-mode-change">
          <ArrowRightLeft aria-hidden="true" />
          <div className="flex min-w-0 flex-1 flex-col gap-2">
            <AlertTitle>Mudança para aprovação {APPROVAL_MODE_LABEL[pending.target_mode].toLowerCase()} em andamento</AlertTitle>
            <AlertDescription className="text-text">
              Solicitada {pending.requested_by_name ? `por ${pending.requested_by_name} ` : ''}em {formatDateTime(pending.requested_at)}; expira em{' '}
              {formatDateTime(pending.expires_at)}.{' '}
              {pending.target_mode === 'SINGLE'
                ? 'Aguardando a aprovação do outro Administrador Master.'
                : 'Falta a autorização de quem solicitou.'}
            </AlertDescription>
            {isMaster ? (
              <Button asChild variant="secondary" className="self-start">
                <Link to={`/financeiro/aprovacoes?pedido=${encodeURIComponent(pending.id)}`}>
                  {pending.target_mode === 'SINGLE' ? 'Abrir o pedido' : 'Abrir e autorizar'}
                </Link>
              </Button>
            ) : null}
          </div>
        </Alert>
      ) : null}

      {isMaster ? (
        <div className="flex flex-col gap-2">
          <Button variant="secondary" className="self-start" disabled={!!pending || blockedByReadiness} onClick={() => setDialogOpen(true)}>
            <ArrowRightLeft aria-hidden="true" />
            Solicitar mudança para {APPROVAL_MODE_LABEL[target]}
          </Button>
          {blockedByReadiness ? (
            <p className="m-0 text-xs text-text-muted">
              Para voltar à aprovação única, o segundo Administrador Master precisa estar ativo: é ele quem aprova a mudança.
            </p>
          ) : null}
          {pending ? <p className="m-0 text-xs text-text-muted">Conclua ou cancele a solicitação em andamento antes de pedir outra.</p> : null}
        </div>
      ) : (
        <p className="m-0 text-sm text-text-muted">Somente os Administradores Master solicitam a mudança do modo de aprovação.</p>
      )}

      {info.history && info.history.length ? (
        <details className="group rounded-lg border border-border">
          <summary className="flex min-h-11 cursor-pointer list-none items-center gap-2 px-4 text-sm font-semibold text-text [&::-webkit-details-marker]:hidden">
            <History aria-hidden="true" className="size-4 text-text-muted" />
            Histórico do modo ({info.history.length})
            <ChevronDown aria-hidden="true" className="ml-auto size-4 text-text-muted transition-transform duration-fast ease-rd group-open:rotate-180" />
          </summary>
          <Separator />
          <ol aria-label="Histórico do modo de aprovação" className="m-0 flex list-none flex-col gap-3 p-4">
            {[...info.history]
              .sort((a, b) => b.mode_version - a.mode_version)
              .map((h) => (
                <li key={h.mode_version} className="flex flex-col gap-0.5 text-sm">
                  <span className="flex flex-wrap items-center gap-2 text-text">
                    <Badge tone="outline">v{h.mode_version}</Badge>
                    <time dateTime={h.created_at} className="tabular-nums text-text-muted">
                      {formatDateTime(h.created_at)}
                    </time>
                  </span>
                  <span className="text-text">{historyLine(h)}</span>
                  {h.reason ? <span className="text-text-muted">Motivo: {h.reason}</span> : null}
                </li>
              ))}
          </ol>
        </details>
      ) : null}

      {isMaster ? <ApprovalModeChangeDialog open={dialogOpen} onOpenChange={setDialogOpen} currentMode={info.mode} /> : null}
    </Card>
  )
}
