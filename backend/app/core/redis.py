"""
RD Mídia Indoor — Redis.

- Pools BLOQUEANTES (``REDIS_MAX_CONNECTIONS``, ``REDIS_POOL_TIMEOUT_S``): rajadas acima
  do limite esperam conexão livre em vez de falhar ("Too many connections").
- Pub/Sub de longa duração deve usar UM cliente dedicado por processo (ver o gateway WS),
  nunca uma assinatura por conexão WebSocket.
- ``get_redis()``: cliente assíncrono **por event loop** (API: um loop; Celery com
  ``asyncio.run`` repetido: um cliente novo por execução — sem "different loop").
- ``get_sync_redis()``: cliente síncrono (thread-safe) para tasks Celery síncronas.
- ``rkey(*parts)``: monta chave com o prefixo da instância/execução
  (``settings.REDIS_KEY_PREFIX``). TODA chave da aplicação deve usar ``rkey`` —
  os testes usam um prefixo exclusivo por execução e limpam ao final.
"""
from __future__ import annotations

import asyncio
import time
import weakref
from typing import Optional

import structlog

import redis as sync_redis
import redis.asyncio as aioredis

from app.core.config import settings

_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, aioredis.Redis]" = weakref.WeakKeyDictionary()
_sync_client: Optional[sync_redis.Redis] = None
_log = structlog.get_logger("rd.redis")

# ─── Disjuntor em processo ───────────────────────────────────────────────────
_down_until: float = 0.0


def redis_breaker_open() -> bool:
    """True durante a janela após uma falha de Redis (chamadas críticas → 503 sem tentar)."""
    return time.monotonic() < _down_until


def mark_redis_down() -> None:
    global _down_until
    _down_until = time.monotonic() + settings.REDIS_BREAKER_S


def mark_redis_up() -> None:
    global _down_until
    _down_until = 0.0


reset_breaker = mark_redis_up


def op_timeout_s() -> float:
    """Limite total de uma operação crítica (inclui resolução de nome, que o socket_timeout
    não cobre): conexão + folga de 0,5 s — mantém a resposta 503 abaixo de 2 s."""
    return settings.REDIS_CONNECT_TIMEOUT_S + 0.5


async def ensure_redis_available() -> None:
    """Lança ``ServiceUnavailable`` (503) em < 2 s se o Redis não responder.

    Usada no início das rotas de autenticação que dependem do Redis (rate limit, desafio
    de 2FA de uso único, tickets de WS) — o erro é claro e NADA é contado no lockout.
    """
    from app.core.errors import ServiceUnavailable

    if redis_breaker_open():
        raise ServiceUnavailable()
    try:
        r = await get_redis()
        await asyncio.wait_for(r.ping(), timeout=op_timeout_s())
    except Exception as exc:
        mark_redis_down()
        _log.error("redis_unavailable", error_type=type(exc).__name__)
        raise ServiceUnavailable() from None
    mark_redis_up()


def rkey(*parts: object) -> str:
    return settings.REDIS_KEY_PREFIX + ":".join(str(p) for p in parts)


def _new_async_client() -> aioredis.Redis:
    # BlockingConnectionPool: com todas as conexões em uso, aguarda uma livre (até
    # REDIS_POOL_TIMEOUT_S) em vez de lançar "Too many connections".
    pool = aioredis.BlockingConnectionPool.from_url(
        settings.REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
        max_connections=settings.REDIS_MAX_CONNECTIONS,
        timeout=settings.REDIS_POOL_TIMEOUT_S,
        socket_connect_timeout=settings.REDIS_CONNECT_TIMEOUT_S,
        socket_timeout=settings.REDIS_SOCKET_TIMEOUT_S,
        health_check_interval=30,
    )
    return aioredis.Redis.from_pool(pool)  # fecha o pool junto com o cliente


async def init_redis() -> None:
    await get_redis()


async def get_redis() -> aioredis.Redis:
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None:
        client = _new_async_client()
        _clients[loop] = client
    return client


def get_sync_redis() -> sync_redis.Redis:
    global _sync_client
    if _sync_client is None:
        pool = sync_redis.BlockingConnectionPool.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            max_connections=settings.REDIS_MAX_CONNECTIONS,
            timeout=settings.REDIS_POOL_TIMEOUT_S,
            socket_connect_timeout=settings.REDIS_CONNECT_TIMEOUT_S,
            socket_timeout=settings.REDIS_SOCKET_TIMEOUT_S,
        )
        _sync_client = sync_redis.Redis(connection_pool=pool)
    return _sync_client


async def close_redis() -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    client = _clients.pop(loop, None)
    if client is not None:
        await client.aclose()
