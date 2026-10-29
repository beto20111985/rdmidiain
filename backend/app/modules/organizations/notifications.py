"""
RD Mídia Indoor — Tipos de notificação do domínio W-ORG (registrados no import).

Entrega via ``app.modules.notifications.service.notify`` (W-BIZ): na transação do
chamador, envio após o commit.
"""
from __future__ import annotations

from app.modules.notifications.catalog import EMAIL, IN_APP, EventType, register_event_type

for _et in [
    EventType("onboarding.submitted", "Cadastro de estabelecimento aguardando aprovação (equipe)",
              "Novo cadastro aguardando aprovação",
              "O cadastro de {{establishment_name}} ({{city}}/{{state}}) aguarda aprovação.",
              ("establishment_name", "city", "state", "application_id"), audience="staff",
              link="/onboarding/{{application_id}}"),
    EventType("contract.acceptance_required", "Contrato aguardando aceite", "Contrato de parceria aguardando aceite",
              "O contrato de parceria de {{establishment_name}} aguarda o seu aceite para ativar as telas.",
              ("establishment_name", "establishment_id"), default_channels=(IN_APP, EMAIL),
              link="/contratos"),
    EventType("installation.assigned", "Instalação designada (técnico)", "Instalação designada",
              "Você foi designado para a instalação em {{establishment_name}} ({{scheduled_date}}).",
              ("establishment_name", "installation_id", "scheduled_date"), audience="staff",
              link="/instalacoes/{{installation_id}}"),
    EventType("maintenance.assigned", "Manutenção designada (técnico)", "Manutenção designada",
              "Manutenção ({{component}}, prioridade {{priority}}) em {{establishment_name}} designada a você.",
              ("establishment_name", "order_id", "component", "priority"), audience="staff",
              link="/manutencao/{{order_id}}"),
]:
    register_event_type(_et)
