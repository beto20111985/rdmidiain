"""
RD Mídia Indoor — Barramento de eventos em tempo real (contrato §5).

Arquitetura: Redis Pub/Sub. QUALQUER processo (API ou worker Celery) publica; cada
processo da API assina e repassa às suas conexões WebSocket, filtrando pelo escopo
do usuário (``visible_to``). Nenhuma dependência de memória do FastAPI.

Publicar (API, async)::

    from app.core import events
    await events.publish("campaign.updated", {"id": str(c.id), "status": c.status},
                         org_id=c.org_id)

Publicar de task Celery síncrona::

    events.publish_sync("media.processing", {"id": ..., "progress": 50}, org_id=org_id)

Canais (todos prefixados por ``REDIS_KEY_PREFIX``):
- ``events:global``            → eventos de plataforma (somente equipe RD com permissão)
- ``events:org:<org_id>``      → eventos de uma organização
- ``events:user:<user_id>``    → eventos pessoais (ex.: notification.created)
- ``events:screen:<screen_id>``→ eventos destinados a um player (agente de playback)
Assinantes da API usam o padrão ``events:*`` (``SUBSCRIBE_PATTERN``) e decidem a
entrega com ``visible_to(ctx, envelope)``.

Envelope publicado (o campo ``scope`` é interno — o WS remove antes de enviar)::

    {"type": "screen.status", "id": "<uuid>", "ts": "<iso Z>", "data": {...},
     "scope": {"org_id":..., "establishment_id":..., "city":..., "user_id":..., "screen_id":...}}

Tickets de WebSocket (uso único, 30 s, guardados como hash no Redis):
``create_ws_ticket(ctx)`` / ``consume_ws_ticket(ticket)``.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from typing import TYPE_CHECKING, Any, Optional

import structlog

from app.core.redis import get_redis, get_sync_redis, rkey
from app.core.timeutils import isoformat_z, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from app.core.authz import AuthContext

log = structlog.get_logger("rd.events")

# Tipos do contrato §5 → permissão necessária para receber (além de escopo).
EVENT_PERMISSIONS: dict[str, Optional[str]] = {
    "screen.status": "screens.view",
    "screen.sync": "screens.view",
    "screen.updated": "screens.view",
    "establishment.updated": "establishments.view",
    "campaign.updated": "campaigns.view",
    "approval.pending": "campaigns.approve",
    "payment.updated": "payments.view",
    "media.processing": "media.view",
    "inventory.updated": "inventory.view",
    "notification.created": None,  # entregue somente ao user_id do escopo
    "finance.approval_request": "finance.approve",
    "withdrawal.updated": "withdrawals.view",
    "ticket.updated": "support.view",
    "designer.updated": "design.view",
}


def register_event_type(name: str, permission: Optional[str]) -> None:
    """Registra um tipo de evento de domínio (sem editar o core).

    ``permission`` = permissão do catálogo exigida para receber (no escopo do evento);
    ``None`` = entregue somente ao ``user_id`` do escopo.
    """
    from app.core.permissions import is_valid_permission

    if not name or "." not in name:
        raise ValueError("nome de evento deve ser 'recurso.evento'")
    if permission is not None and not is_valid_permission(permission):
        raise ValueError(f"Permissão fora do catálogo: {permission}")
    current = EVENT_PERMISSIONS.get(name, ...)
    if current is not ... and current != permission:
        raise ValueError(f"evento {name} já registrado com outra permissão ({current})")
    EVENT_PERMISSIONS[name] = permission


def event_types() -> frozenset[str]:
    return frozenset(EVENT_PERMISSIONS)

WS_TICKET_TTL_S = 30
SUBSCRIBE_PATTERN_SUFFIX = "events:*"


def channel_global() -> str:
    return rkey("events", "global")


def channel_org(org_id: Any) -> str:
    return rkey("events", "org", org_id)


def channel_user(user_id: Any) -> str:
    return rkey("events", "user", user_id)


def channel_screen(screen_id: Any) -> str:
    return rkey("events", "screen", screen_id)


def subscribe_pattern() -> str:
    return rkey("events", "*")


def _s(v: Any) -> Optional[str]:
    return None if v is None else str(v)


def build_envelope(
    type: str,
    data: dict,
    *,
    org_id: Any = None,
    establishment_id: Any = None,
    city: Optional[str] = None,
    user_id: Any = None,
    screen_id: Any = None,
) -> dict:
    if type not in EVENT_PERMISSIONS:
        raise ValueError(f"Tipo de evento desconhecido: {type}")
    return {
        "type": type,
        "id": str(uuid.uuid4()),
        "ts": isoformat_z(utcnow()),
        "data": data,
        "scope": {
            "org_id": _s(org_id),
            "establishment_id": _s(establishment_id),
            "city": city,
            "user_id": _s(user_id),
            "screen_id": _s(screen_id),
        },
    }


def channels_for(envelope: dict) -> list[str]:
    sc = envelope["scope"]
    chans: list[str] = []
    if sc.get("user_id"):
        chans.append(channel_user(sc["user_id"]))
    if sc.get("screen_id"):
        chans.append(channel_screen(sc["screen_id"]))
    if sc.get("org_id"):
        chans.append(channel_org(sc["org_id"]))
    if not chans or (not sc.get("user_id") and not sc.get("screen_id") and not sc.get("org_id")):
        chans.append(channel_global())
    return chans


async def publish(type: str, data: dict, **scope: Any) -> dict:
    """Publica um evento (async). Retorna o envelope publicado."""
    env = build_envelope(type, data, **scope)
    payload = json.dumps(env, default=str, separators=(",", ":"))
    r = await get_redis()
    for ch in channels_for(env):
        await r.publish(ch, payload)
    return env


def publish_sync(type: str, data: dict, **scope: Any) -> dict:
    """Publica um evento a partir de código síncrono (tasks Celery)."""
    env = build_envelope(type, data, **scope)
    payload = json.dumps(env, default=str, separators=(",", ":"))
    r = get_sync_redis()
    for ch in channels_for(env):
        r.publish(ch, payload)
    return env


def public_envelope(env: dict) -> dict:
    """Envelope do contrato (sem o escopo interno) para enviar ao cliente."""
    return {k: env[k] for k in ("type", "id", "ts", "data")}


def visible_to(ctx: "AuthContext", env: dict) -> bool:
    """Decide se um evento pode ser entregue à conexão do usuário ``ctx``."""
    sc = env.get("scope") or {}
    etype = env.get("type")
    if sc.get("user_id"):
        return ctx.user_id is not None and str(ctx.user_id) == sc["user_id"]
    perm = EVENT_PERMISSIONS.get(etype)
    if perm is None:
        return False
    if not (sc.get("org_id") or sc.get("establishment_id") or sc.get("city")):
        # evento de plataforma: somente quem tem a permissão em escopo GLOBAL
        return ctx.can(perm, global_only=True)
    return ctx.can(perm, org_id=sc.get("org_id"), establishment_id=sc.get("establishment_id"), city=sc.get("city"))


# ─── Tickets de WebSocket ───────────────────────────────────────────────────


def _ticket_key(ticket: str) -> str:
    return rkey("wsticket", hashlib.sha256(ticket.encode()).hexdigest())


async def create_ws_ticket(ctx: "AuthContext", ttl_s: int = WS_TICKET_TTL_S) -> str:
    ticket = secrets.token_urlsafe(32)
    info = {
        "user_id": _s(ctx.user_id),
        "session_id": _s(ctx.session_id),
        "imp_by": _s(ctx.impersonation.real_user_id) if ctx.impersonation else None,
    }
    r = await get_redis()
    await r.set(_ticket_key(ticket), json.dumps(info), ex=ttl_s, nx=True)
    return ticket


async def consume_ws_ticket(ticket: str) -> Optional[dict]:
    """Consome o ticket (uso único: GETDEL). Retorna os dados ou None."""
    if not ticket or len(ticket) > 128:
        return None
    r = await get_redis()
    raw = await r.getdel(_ticket_key(ticket))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None
