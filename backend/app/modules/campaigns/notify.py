"""
RD Mídia Indoor — Eventos de tempo real publicados SOMENTE APÓS o commit.

``defer_event(db, "campaign.updated", data, org_id=...)`` enfileira o evento na sessão;
um listener ``after_commit`` publica via ``app.core.events.publish_sync`` (Redis Pub/Sub,
funciona na API e no worker). Em rollback a fila é descartada — nenhum evento de algo
que não aconteceu (sem "sucesso falso"). Falha do Redis não desfaz a transação já
confirmada: é registrada em log (o frontend refaz a consulta na reconexão do WS).
"""
from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events

log = structlog.get_logger("rd.campaigns.events")

_KEY = "rd_campaign_pending_events"
_LISTENING = "rd_campaign_events_listener"


def _publish_pending(session) -> None:
    pending = session.info.pop(_KEY, [])
    for etype, data, scope in pending:
        if etype == _PLAYER:
            _publish_player(data, scope)
            continue
        try:
            events.publish_sync(etype, data, **scope)
        except Exception as exc:  # pragma: no cover - Redis indisponível
            log.error("event_publish_failed", event_type=etype, error_type=type(exc).__name__, exc_info=exc)


def _discard_pending(session) -> None:
    session.info.pop(_KEY, None)


def defer_event(db: AsyncSession, etype: str, data: dict, **scope: Any) -> None:
    events.build_envelope(etype, data, **scope)  # valida tipo/escopo já aqui
    sync = db.sync_session
    sync.info.setdefault(_KEY, []).append((etype, data, scope))
    if not sync.info.get(_LISTENING):
        event.listen(sync, "after_commit", _publish_pending)
        event.listen(sync, "after_rollback", _discard_pending)
        sync.info[_LISTENING] = True


_PLAYER = "__player__"


def _publish_player(data: dict, scope: dict) -> None:
    try:
        from app.modules.websocket import bus

        for sid in scope.get("screen_ids") or []:
            bus.to_screen_sync(sid, scope["type"], data)
    except Exception as exc:  # pragma: no cover - Redis indisponível (manifesto também revoga)
        log.error("player_publish_failed", type=scope.get("type"), error_type=type(exc).__name__)


def defer_player_message(db: AsyncSession, screen_ids, ptype: str, data: dict) -> None:
    """Mensagem RDPP aos players das telas (ex.: ``kill``) publicada SOMENTE após o commit."""
    from app.modules.websocket import bus

    bus.envelope(ptype, data)  # valida o tipo já aqui
    sync = db.sync_session
    sync.info.setdefault(_KEY, []).append((_PLAYER, data, {"screen_ids": [str(x) for x in screen_ids],
                                                           "type": ptype}))
    if not sync.info.get(_LISTENING):
        event.listen(sync, "after_commit", _publish_pending)
        event.listen(sync, "after_rollback", _discard_pending)
        sync.info[_LISTENING] = True


def pending_events(db: AsyncSession) -> list:
    return list(db.sync_session.info.get(_KEY, []))


def mark(db: AsyncSession) -> int:
    """Posição atual da fila (para descartar eventos de um SAVEPOINT desfeito)."""
    return len(db.sync_session.info.get(_KEY, []))


def rollback_to(db: AsyncSession, position: int) -> None:
    q = db.sync_session.info.get(_KEY)
    if q is not None:
        del q[position:]
