"""Configurações do domínio de campanhas/conflitos/targeting (registry de ``settings_service``)."""
from __future__ import annotations

from app.core.settings_service import REGISTRY, SettingDef, register

_DEFS = [
    SettingDef("campaign.review_sla_hours", 24, "int", "SLA (horas) para a RD analisar uma campanha em EM_ANALISE.",
               min=1, max=720, category="campanhas"),
    SettingDef("campaign.playing_window_minutes", 15, "int",
               "Janela (min) de PoP válido para exibir a campanha como REPRODUZINDO.", min=1, max=1440,
               category="campanhas"),
    SettingDef("targeting.reveal_establishment_names", False, "bool",
               "Revelar nomes de estabelecimentos ao anunciante antes da contratação (padrão: oculto).",
               category="campanhas"),
    SettingDef("campaign.renewal_notice_days", 5, "int",
               "Dias antes do vencimento local em que a renovação automática gera a cobrança (D-N).",
               min=1, max=30, category="campanhas"),
    # campaign.renewal_hold_hours REMOVIDA: renovação usa a reserva padrão de 2 h no checkout.
    SettingDef("conflicts.same_network_allow", True, "bool",
               "Anúncio da própria rede (mesma organização) sempre permitido (A6 §10.7 passo 1).",
               category="campanhas"),
]

for _d in _DEFS:
    if _d.key not in REGISTRY:
        register(_d)


def _register_notification_types() -> None:
    try:
        from app.modules.notifications.catalog import REGISTRY as NREG
        from app.modules.notifications.catalog import EventType, register_event_type
    except Exception:  # pragma: no cover - módulo de notificações ausente
        return
    et = EventType(
        "campaign.renewal_available", "Renovação de campanha disponível", "Renovação disponível: {{campaign_name}}",
        "A renovação da campanha {{campaign_name}} até {{new_end_date}} está disponível no valor de {{amount}}. "
        "Gere o pagamento no painel até {{expires_at}} para continuar sem interrupção; sem pagamento, a campanha "
        "encerra no vencimento.",
        ("campaign_name", "campaign_id", "new_end_date", "amount", "amount_cents", "expires_at"),
        link="/campanhas/{{campaign_id}}",
    )
    if et.key not in NREG:
        register_event_type(et)
    et2 = EventType(
        "campaign.change_no_capacity", "Alteração sem capacidade", "Alteração não aprovada: {{campaign_name}}",
        "A alteração da campanha {{campaign_name}} não pôde ser aprovada: não há capacidade nas telas para a nova "
        "configuração. A versão vigente continua no ar.",
        ("campaign_name", "campaign_id"), link="/campanhas/{{campaign_id}}",
    )
    if et2.key not in NREG:
        register_event_type(et2)


_register_notification_types()
