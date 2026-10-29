"""
RD Mídia Indoor — Efeitos pós-commit do inventário (eventos WS, tarefas) e notificações.

- ``defer_event``: evento de tempo real publicado SOMENTE após o commit (rollback
  descarta) — via ``app.core.events.publish_sync`` (API e worker).
- ``after_commit``: callable síncrono executado após o commit (ex.: enfileirar task).
- ``notify_safe``: notificação in-app/e-mail pelo serviço do W-BIZ, na transação
  corrente; tipos do inventário registrados aqui.
"""
from __future__ import annotations

from typing import Any, Callable

import structlog
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events

log = structlog.get_logger("rd.inventory.events")

_EV_KEY = "rd_inventory_pending_events"
_FN_KEY = "rd_inventory_after_commit"
_LISTENING = "rd_inventory_listener"


def _on_commit(session) -> None:
    pending = session.info.pop(_EV_KEY, [])
    fns = session.info.pop(_FN_KEY, [])
    for etype, data, scope in pending:
        try:
            events.publish_sync(etype, data, **scope)
        except Exception as exc:  # pragma: no cover - Redis indisponível (dado já confirmado)
            log.error("event_publish_failed", event_type=etype, error_type=type(exc).__name__)
    for fn in fns:
        try:
            fn()
        except Exception as exc:  # pragma: no cover
            log.error("after_commit_failed", error_type=type(exc).__name__)


def _on_rollback(session) -> None:
    session.info.pop(_EV_KEY, None)
    session.info.pop(_FN_KEY, None)


def _listen(db: AsyncSession):
    sync = db.sync_session
    if not sync.info.get(_LISTENING):
        event.listen(sync, "after_commit", _on_commit)
        event.listen(sync, "after_rollback", _on_rollback)
        sync.info[_LISTENING] = True
    return sync


def defer_event(db: AsyncSession, etype: str, data: dict, **scope: Any) -> None:
    events.build_envelope(etype, data, **scope)  # valida tipo/escopo já aqui
    _listen(db).info.setdefault(_EV_KEY, []).append((etype, data, scope))


def after_commit(db: AsyncSession, fn: Callable[[], None]) -> None:
    _listen(db).info.setdefault(_FN_KEY, []).append(fn)


def pending_events(db: AsyncSession) -> list:
    return list(db.sync_session.info.get(_EV_KEY, []))


# ─── Notificações (W-BIZ) ────────────────────────────────────────────────────

def _register_types() -> bool:
    try:
        from app.modules.notifications.catalog import IN_APP, EMAIL, EventType, register_event_type
    except ImportError:  # pragma: no cover
        return False
    for et in (
        EventType("inventory.hold_expired", "Reserva de inventário expirada", "Reserva expirada",
                  "A reserva das telas da campanha {{campaign_name}} expirou sem pagamento e foi liberada.",
                  ("campaign_name", "campaign_id", "hold_id"), (IN_APP, EMAIL), audience="client",
                  link="/campanhas/{{campaign_id}}"),
        EventType("inventory.paid_without_inventory", "Pagamento sem inventário (crítico)",
                  "Pagamento sem inventário: {{campaign_name}}",
                  "O pagamento da campanha {{campaign_name}} chegou após a reserva expirar e não há mais "
                  "capacidade nas telas contratadas. Nenhum overbooking foi feito; inicie o estorno.",
                  ("campaign_name", "campaign_id", "hold_id", "order_id"), (IN_APP, EMAIL), critical=True,
                  audience="staff", link="/financeiro"),
        EventType("inventory.screen_retired", "Tela removida com contratações", "Tela removida: {{screen_code}}",
                  "A tela {{screen_code}} foi removida definitivamente com {{affected}} contratação(ões) ativas. "
                  "Decida a redistribuição/reembolso no relatório.",
                  ("screen_code", "screen_id", "affected", "report_id"), (IN_APP, EMAIL), audience="staff",
                  link="/inventario/redistribuicao"),
        EventType("inventory.emergency_published", "Comunicado de emergência publicado",
                  "Emergência publicada: {{title}}",
                  "Comunicado de emergência \"{{title}}\" publicado para {{screens}} tela(s).",
                  ("title", "screens", "emergency_id"), (IN_APP,), audience="staff", link="/emergencias"),
        EventType("inventory.unplaced_insertions", "Inserções contratadas sem posição", "Manifesto incompleto: {{screen_code}}",
                  "O manifesto v{{version}} da tela {{screen_code}} não conseguiu posicionar {{count}} inserção(ões) "
                  "contratada(s). Verifique conflitos/política.",
                  ("screen_code", "screen_id", "version", "count"), (IN_APP,), audience="staff",
                  default_cooldown_s=6 * 3600, link="/telas"),
    ):
        try:
            register_event_type(et)
        except ValueError as exc:  # pragma: no cover - registro conflitante
            log.error("notification_type_register_failed", key=et.key, error=str(exc))
    return True


_TYPES_OK = _register_types()


async def notify_safe(db: AsyncSession, recipients: Any, event_type: str, data: dict, **kw: Any) -> bool:
    if not _TYPES_OK:
        log.warning("notifications_unavailable", event_type=event_type)
        return False
    from app.modules.notifications.service import notify

    try:
        async with db.begin_nested():
            await notify(db, recipients, event_type, data, **kw)
        return True
    except Exception as exc:  # notificação nunca derruba a operação principal
        log.error("notify_failed", event_type=event_type, error_type=type(exc).__name__)
        return False


def staff(permission: str = "inventory.view"):
    from app.modules.notifications.service import StaffRecipients

    return StaffRecipients(permission)


def org(org_id: Any, permission: str = "campaigns.view"):
    from app.modules.notifications.service import OrgRecipients

    return OrgRecipients(org_id, permission)
