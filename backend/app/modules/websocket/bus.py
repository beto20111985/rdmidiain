"""
RD Mídia Indoor — Barramento de mensagens para PLAYERS (RDPP/1 §4) via Redis Pub/Sub.

Qualquer processo (API com N workers, worker Celery, CLI) publica; cada processo da API
entrega às conexões WebSocket que mantém (``app.modules.websocket.gateway``).

Canais (prefixados por ``REDIS_KEY_PREFIX``):
- ``rdpp:dev:<device_id>``  → um dispositivo (``command``, ``device_revoked``, ``flush_events``, ``server_time``)
- ``rdpp:scr:<screen_id>``  → o(s) player(s) de uma tela (``sync_required``, ``kill``)
- ``rdpp:all``              → todos os players conectados (``kill`` global)
- ``rdpp:pair:<request_id>``→ WS de pareamento (``pairing_claimed``, ``pairing_expired``)

Eventos do painel (``app.core.events``) com tipo ``screen.sync`` publicados no canal
``events:screen:<id>`` (W-INV ``invalidate_screens``) são traduzidos pelo gateway para
``sync_required {version}`` ao player daquela tela.

Envelope entregue (RDPP §4): ``{"type","id","ts_ms","data"}`` — tipos minúsculos com ``_``.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterable

from app.core.redis import get_redis, get_sync_redis, rkey

PLAYER_TYPES = frozenset(
    {
        "server_time",
        "sync_required",
        "command",
        "kill",
        "device_revoked",
        "flush_events",
        "pairing_claimed",
        "pairing_expired",
    }
)


def now_ms() -> int:
    return int(time.time() * 1000)


def ch_device(device_id: Any) -> str:
    return rkey("rdpp", "dev", device_id)


def ch_screen(screen_id: Any) -> str:
    return rkey("rdpp", "scr", screen_id)


def ch_all() -> str:
    return rkey("rdpp", "all")


def ch_pairing(request_id: Any) -> str:
    return rkey("rdpp", "pair", request_id)


def pattern() -> str:
    return rkey("rdpp", "*")


def envelope(type: str, data: dict | None = None) -> dict:
    if type not in PLAYER_TYPES:
        raise ValueError(f"Tipo de mensagem do player desconhecido: {type}")
    return {"type": type, "id": str(uuid.uuid4()), "ts_ms": now_ms(), "data": data or {}}


def _payload(type: str, data: dict | None) -> str:
    return json.dumps(envelope(type, data), separators=(",", ":"), default=str)


async def publish(channel: str, type: str, data: dict | None = None) -> int:
    r = await get_redis()
    return int(await r.publish(channel, _payload(type, data)))


def publish_sync(channel: str, type: str, data: dict | None = None) -> int:
    """Versão síncrona (tasks Celery)."""
    return int(get_sync_redis().publish(channel, _payload(type, data)))


async def to_device(device_id: Any, type: str, data: dict | None = None) -> int:
    return await publish(ch_device(device_id), type, data)


async def to_screen(screen_id: Any, type: str, data: dict | None = None) -> int:
    return await publish(ch_screen(screen_id), type, data)


async def to_screens(screen_ids: Iterable[Any], type: str, data: dict | None = None) -> None:
    r = await get_redis()
    pipe = r.pipeline(transaction=False)
    for sid in screen_ids:
        pipe.publish(ch_screen(sid), _payload(type, data))
    await pipe.execute()


async def to_all(type: str, data: dict | None = None) -> int:
    return await publish(ch_all(), type, data)


async def to_pairing(request_id: Any, type: str, data: dict | None = None) -> int:
    return await publish(ch_pairing(request_id), type, data)


def to_screen_sync(screen_id: Any, type: str, data: dict | None = None) -> int:
    return publish_sync(ch_screen(screen_id), type, data)


def to_device_sync(device_id: Any, type: str, data: dict | None = None) -> int:
    return publish_sync(ch_device(device_id), type, data)
