"""
Catálogo de tipos de evento de notificação (ETAPA2 #815; P001 §105).

Cada tipo declara: variáveis permitidas nos templates, canais padrão, cooldown
padrão (anti-spam), textos padrão (pt-BR) e se é crítico (IN_APP não pode ser
desligado pelo usuário). Outros domínios podem registrar tipos próprios com
``register_event_type(EventType(...))`` no import do seu módulo.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

IN_APP, EMAIL, PUSH = "IN_APP", "EMAIL", "PUSH"


@dataclass(frozen=True)
class EventType:
    key: str
    label: str
    title: str
    body: str
    variables: tuple[str, ...] = ()
    default_channels: tuple[str, ...] = (IN_APP, EMAIL)
    default_cooldown_s: int = 0
    critical: bool = False
    audience: str = "client"  # client | staff | any (informativo para a UI de preferências)
    link: Optional[str] = None  # caminho do frontend (pode conter {{variaveis}})
    extra: dict = field(default_factory=dict)


REGISTRY: dict[str, EventType] = {}


def register_event_type(et: EventType) -> EventType:
    from app.modules.notifications.templating import validate_template

    existing = REGISTRY.get(et.key)
    if existing is not None and existing != et:
        raise ValueError(f"tipo de notificação já registrado com outra definição: {et.key}")
    for ch in et.default_channels:
        if ch not in (IN_APP, EMAIL, PUSH):
            raise ValueError(f"canal inválido em {et.key}: {ch}")
    validate_template(et.title, et.variables)
    validate_template(et.body, et.variables)
    REGISTRY[et.key] = et
    return et


def get_event_type(key: str) -> EventType:
    et = REGISTRY.get(key)
    if et is None:
        raise ValueError(f"tipo de notificação desconhecido: {key}")
    return et


_H = 3600
for _et in [
    # ── pagamentos / campanhas (ETAPA2 #815) ──
    EventType("payment.approved", "Pagamento aprovado", "Pagamento aprovado",
              "O pagamento {{reference}} no valor de {{amount}} foi aprovado.",
              ("reference", "amount", "campaign_name"), link="/financeiro"),
    EventType("campaign.awaiting_approval", "Campanha aguardando aprovação (equipe)", "Campanha aguardando aprovação",
              "A campanha {{campaign_name}} aguarda revisão da equipe {{instance_name}}.", ("campaign_name", "campaign_id"),
              audience="staff", link="/campanhas/{{campaign_id}}"),
    EventType("campaign.approved", "Campanha aprovada", "Campanha aprovada",
              "Sua campanha {{campaign_name}} foi aprovada.", ("campaign_name", "campaign_id"),
              link="/campanhas/{{campaign_id}}"),
    EventType("campaign.rejected", "Campanha rejeitada", "Campanha rejeitada",
              "Sua campanha {{campaign_name}} foi rejeitada. Motivo: {{reason}}", ("campaign_name", "campaign_id", "reason"),
              link="/campanhas/{{campaign_id}}"),
    EventType("campaign.started", "Campanha iniciada", "Campanha iniciada",
              "A campanha {{campaign_name}} começou a ser veiculada.", ("campaign_name", "campaign_id")),
    EventType("campaign.ended", "Campanha encerrada", "Campanha encerrada",
              "A campanha {{campaign_name}} foi encerrada.", ("campaign_name", "campaign_id")),
    # ── telas (cooldown — ETAPA2 #819) ──
    EventType("screen.offline", "Tela offline", "Tela offline: {{screen_label}}",
              "A tela {{screen_label}} está offline desde {{offline_since}}.",
              ("screen_code", "screen_name", "screen_label", "offline_since", "days_offline"),
              default_channels=(IN_APP, EMAIL, PUSH), default_cooldown_s=6 * _H, link="/telas"),
    EventType("screen.outdated", "Tela desatualizada", "Tela desatualizada: {{screen_label}}",
              "A tela {{screen_label}} não sincroniza a programação mais recente desde {{since}}.",
              ("screen_code", "screen_name", "screen_label", "since"), default_cooldown_s=12 * _H, link="/telas"),
    # ── financeiro do estabelecimento ──
    EventType("withdrawal.requested", "Saque solicitado", "Saque solicitado",
              "Seu saque de {{amount}} foi solicitado e está em processamento.", ("amount",), link="/financeiro/saques"),
    EventType("withdrawal.paid", "Saque pago", "Saque pago", "Seu saque de {{amount}} foi pago.", ("amount",),
              link="/financeiro/saques"),
    EventType("withdrawal.failed", "Saque não concluído", "Saque não concluído",
              "Seu saque de {{amount}} não foi concluído. Motivo: {{reason}}", ("amount", "reason"), critical=True,
              link="/financeiro/saques"),
    EventType("balance.available", "Saldo disponível", "Saldo disponível",
              "Você tem {{amount}} de saldo disponível.", ("amount",), default_cooldown_s=24 * _H,
              link="/financeiro"),
    EventType("pix_key.changed", "Chave PIX alterada", "Chave PIX alterada",
              "A chave PIX de recebimento foi alterada. Saques ficam bloqueados até {{blocked_until}}.",
              ("blocked_until",), critical=True, link="/financeiro"),
    # ── mídia ──
    EventType("media.error", "Mídia com erro", "Problema no processamento da mídia",
              "O arquivo {{filename}} não pôde ser processado: {{reason}}", ("filename", "reason")),
    # ── designer (ETAPA2 #378–#387) ──
    EventType("design.request_created", "Novo pedido de arte (equipe)", "Novo pedido de arte {{request_code}}",
              "Pedido de arte {{request_code}} — {{title}} entrou na fila.", ("request_code", "title", "request_id"),
              audience="staff", link="/design/{{request_id}}"),
    EventType("design.version_ready", "Arte pronta para revisão", "Nova versão da arte {{request_code}}",
              "A versão {{version}} da arte {{title}} está pronta para sua revisão.",
              ("request_code", "title", "version", "request_id"), link="/design/{{request_id}}"),
    EventType("design.changes_requested", "Alteração solicitada (equipe)", "Alteração solicitada em {{request_code}}",
              "O cliente pediu alteração na versão {{version}}: {{comment}}",
              ("request_code", "version", "comment", "request_id"), audience="staff", link="/design/{{request_id}}"),
    EventType("design.approved", "Arte aprovada", "Arte {{request_code}} aprovada",
              "A versão {{version}} da arte {{title}} foi aprovada.", ("request_code", "title", "version", "request_id"),
              audience="any", link="/design/{{request_id}}"),
    EventType("design.published", "Arte publicada", "Arte {{request_code}} publicada",
              "A arte {{title}} foi publicada na biblioteca de mídias.", ("request_code", "title", "request_id"),
              link="/design/{{request_id}}"),
    EventType("design.revision_limit", "Limite de revisões atingido", "Limite de revisões atingido",
              "O pedido {{request_code}} usou as {{included}} revisões incluídas. Novas alterações exigem contratação adicional.",
              ("request_code", "included", "request_id"), link="/design/{{request_id}}"),
    # ── suporte ──
    EventType("support.ticket_created", "Novo chamado (equipe)", "Novo chamado {{ticket_number}}",
              "{{title}}", ("ticket_number", "title", "ticket_id"), audience="staff", link="/suporte/{{ticket_id}}"),
    EventType("support.ticket_updated", "Chamado atualizado", "Chamado {{ticket_number}} atualizado",
              "Status: {{status}}.", ("ticket_number", "status", "ticket_id"), link="/suporte/{{ticket_id}}"),
    EventType("support.ticket_reply", "Nova resposta no chamado", "Nova resposta no chamado {{ticket_number}}",
              "{{excerpt}}", ("ticket_number", "excerpt", "ticket_id"), audience="any", link="/suporte/{{ticket_id}}"),
    # ── CRM ──
    EventType("crm.discount_approval_requested", "Desconto aguardando aprovação (equipe)",
              "Desconto aguardando aprovação — {{proposal_number}}",
              "A proposta {{proposal_number}} tem desconto de {{discount}} acima do limite de {{limit}}.",
              ("proposal_number", "discount", "limit", "proposal_id"), audience="staff", link="/crm/propostas/{{proposal_id}}"),
    EventType("crm.discount_decided", "Desconto decidido (equipe)", "Desconto {{decision}} — {{proposal_number}}",
              "O desconto da proposta {{proposal_number}} foi {{decision}}.",
              ("proposal_number", "decision", "proposal_id"), audience="staff", link="/crm/propostas/{{proposal_id}}"),
    EventType("crm.lead_inbound", "Novo lead pelo auto-cadastro (equipe)", "Novo lead: {{name}}",
              "{{company}} ({{city}}) pediu para falar com o comercial (origem: {{source}}).",
              ("lead_id", "name", "company", "city", "source"), audience="staff", link="/crm/leads/{{lead_id}}"),
    # ── relatórios ──
    EventType("report.ready", "Relatório pronto", "Relatório pronto", "O relatório {{report_name}} está disponível para download.",
              ("report_name", "export_id"), default_channels=(IN_APP,), audience="any", link="/relatorios"),
    EventType("report.monthly", "Relatório mensal", "Relatório mensal — {{period}}",
              "O relatório mensal de {{period}} foi enviado para o seu e-mail.", ("period",), default_channels=(IN_APP,),
              link="/relatorios"),
    # ── control plane ──
    EventType("control_plane.rollout_finished", "Rollout finalizado (equipe)", "Rollout {{version}}: {{status}}",
              "Rollout da versão {{version}} terminou com status {{status}}.", ("version", "status", "rollout_id"),
              default_channels=(IN_APP,), audience="staff", link="/control-plane"),
    # ── genérico (aviso administrativo) ──
    EventType("system.announcement", "Aviso da plataforma", "{{title}}", "{{message}}", ("title", "message"),
              default_channels=(IN_APP,), audience="any"),
]:
    register_event_type(_et)
