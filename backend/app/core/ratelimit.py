"""
RD Mídia Indoor — Rate limit simples por janela fixa (Redis).

Defesa complementar ao nginx (por IP) e ao lockout por conta (persistido no banco).
Redis indisponível → **503 SERVICE_UNAVAILABLE** com Retry-After (fail-closed, P002 §50:
erro claro, nunca parecer credencial errada). ``fail_open=True`` só para usos não críticos.
"""
from __future__ import annotations

import hashlib

import structlog

from app.core.config import settings
import asyncio

from app.core.errors import RateLimited, ServiceUnavailable
from app.core.redis import get_redis, mark_redis_down, op_timeout_s, redis_breaker_open, rkey

log = structlog.get_logger("rd.ratelimit")


def _k(bucket: str, ident: str) -> str:
    return rkey("rl", bucket, hashlib.sha256(ident.encode("utf-8")).hexdigest()[:32])


async def hit(bucket: str, ident: str, limit: int, window_s: int, *, fail_open: bool = False) -> tuple[bool, int]:
    """Conta uma ocorrência. Retorna (permitido, retry_after_s). Redis fora → 503 (ou libera se fail_open)."""
    if not settings.RATE_LIMIT_ENABLED:
        return True, 0
    if redis_breaker_open():
        if fail_open:
            return True, 0
        raise ServiceUnavailable()
    key = _k(bucket, ident)
    try:
        r = await get_redis()
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, window_s, nx=True)
        pipe.ttl(key)
        count, _, ttl = await asyncio.wait_for(pipe.execute(), timeout=op_timeout_s())
    except Exception as exc:
        mark_redis_down()
        log.error("ratelimit_redis_error", bucket=bucket, error_type=type(exc).__name__)
        if fail_open:
            return True, 0
        raise ServiceUnavailable() from None
    if int(count) > limit:
        return False, max(int(ttl), 1)
    return True, 0


async def enforce(bucket: str, ident: str, limit: int, window_s: int, *, fail_open: bool = False) -> None:
    ok, retry = await hit(bucket, ident, limit, window_s, fail_open=fail_open)
    if not ok:
        raise RateLimited(details={"retry_after_s": retry}, headers={"Retry-After": str(retry)})
