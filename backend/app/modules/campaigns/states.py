"""
RD Mídia Indoor — Máquina de estados da campanha (A6 §10.5.1; P001 §39; ADR_006 #10).

Cada transição é (de, para, gatilho). Toda mudança de status passa por
``app.modules.campaigns.service._transition`` com guarda ``UPDATE … WHERE status = :esperado``,
histórico append-only (``campaign_status_history``), auditoria e evento ``campaign.updated``.

REPRODUZINDO não é status: é derivado de PoP recente (``service.playing_state``).
"""
from __future__ import annotations

from app.models.campaigns import CampaignStatus as S

R = S.RASCUNHO.value
AP = S.AGUARDANDO_PAGAMENTO.value
AM = S.AGUARDANDO_MIDIA.value
EA = S.EM_ANALISE.value
APR = S.APROVADA.value
AG = S.AGENDADA.value
SY = S.SINCRONIZANDO.value
AT = S.ATIVA.value
PA = S.PAUSADA.value
FI = S.FINALIZADA.value
RE = S.REJEITADA.value
CA = S.CANCELADA.value

TERMINAL = frozenset({FI, CA})
# Estados em que existe uma versão aprovada que pode tocar (com live_revision_id).
OPERATIONAL = frozenset({APR, AG, SY, AT, PA})
# Estados em que o player pode exibir a versão vigente (W-INV / manifesto).
PLAYABLE = frozenset({APR, AG, SY, AT})
# Estados em que a especificação de trabalho é editável.
EDITABLE = frozenset({R})

# gatilho → conjunto de (de, para)
TRANSITIONS: dict[str, frozenset[tuple[str, str]]] = {
    "checkout": frozenset({(R, AP), (RE, AP)}),
    "payment_expired": frozenset({(AP, R)}),
    "order_cancelled": frozenset({(AP, R)}),
    # pagamento confirmado (também pagamento tardio após a expiração da reserva)
    "payment_confirmed": frozenset({(AP, AM), (AP, EA), (AP, APR), (R, AM), (R, EA), (R, APR)}),
    "media_submitted": frozenset({(AM, EA), (AM, APR)}),
    "approve": frozenset({(EA, APR)}),
    "reject": frozenset({(EA, RE)}),
    "resubmit": frozenset({(RE, EA), (RE, APR)}),
    # alteração pós-aprovação: volta para análise mantendo a versão vigente
    "change_requested": frozenset({(APR, EA), (AG, EA), (SY, EA), (AT, EA), (PA, EA)}),
    # alteração rejeitada/retirada: volta ao estado operacional anterior (versão vigente intacta)
    "change_rejected": frozenset({(EA, APR), (EA, AG), (EA, SY), (EA, AT), (EA, PA)}),
    "mark_scheduled": frozenset({(APR, AG)}),
    "mark_syncing": frozenset({(APR, SY), (AG, SY)}),
    "ack_active": frozenset({(SY, AT), (AG, AT), (APR, AT)}),
    "pause": frozenset({(AG, PA), (SY, PA), (AT, PA)}),
    "kill": frozenset({(APR, PA), (AG, PA), (SY, PA), (AT, PA)}),
    "resume": frozenset({(PA, AT), (PA, AG), (PA, SY), (PA, APR)}),
    "expired": frozenset({(APR, FI), (AG, FI), (SY, FI), (AT, FI), (PA, FI)}),
    "cancel": frozenset({(R, CA), (AP, CA), (AM, CA), (EA, CA), (RE, CA), (APR, CA), (AG, CA), (SY, CA),
                         (AT, CA), (PA, CA)}),
}

ALL_EDGES: frozenset[tuple[str, str]] = frozenset().union(*TRANSITIONS.values())


def is_allowed(trigger: str, from_status: str, to_status: str) -> bool:
    return (from_status, to_status) in TRANSITIONS.get(trigger, frozenset())


def allowed_targets(from_status: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for trig, edges in TRANSITIONS.items():
        tos = sorted(t for f, t in edges if f == from_status)
        if tos:
            out[trig] = tos
    return out
