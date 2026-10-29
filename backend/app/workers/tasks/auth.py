"""
RD Mídia Indoor — Tasks Celery da fundação de autenticação.

Padrão para TODAS as tasks assíncronas (agentes de domínio, sigam este modelo):

    @celery_app.task(name="app.workers.tasks.<modulo>.<nome>", bind=True)
    def minha_task(self, ...):
        return run_async(_minha_task_async(...))

    async def _minha_task_async(...):
        async with worker_session() as db:      # engine NullPool por execução
            ...                                  # commit no sucesso / rollback no erro
        await events.publish(...)                # ou events.publish_sync em código síncrono

``run_async`` usa ``asyncio.run`` (um loop novo por execução); como o engine é
criado e descartado dentro da execução, a mesma task roda N vezes no mesmo processo
sem o erro "attached to a different loop" (A2 E28).
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Coroutine

from celery.utils.log import get_task_logger

from app.workers.celery_app import celery_app

logger = get_task_logger(__name__)


def run_async(coro: Coroutine) -> Any:
    return asyncio.run(coro)


@celery_app.task(name="app.workers.tasks.auth.purge_auth_artifacts", bind=True)
def purge_auth_artifacts(self) -> dict:
    """Remove artefatos de autenticação expirados (refresh tokens, códigos de reset/convite)."""
    result = run_async(_purge_auth_artifacts())
    logger.info("purge_auth_artifacts %s", result)
    return result


async def _purge_auth_artifacts() -> dict:
    from sqlalchemy import delete, update

    from app.core.database import worker_session
    from app.core.timeutils import utcnow
    from app.models.users import PasswordResetCode, RefreshToken, UserSession

    now = utcnow()
    grace = now - timedelta(days=1)
    async with worker_session() as db:
        rt = await db.execute(delete(RefreshToken).where(RefreshToken.expires_at < grace))
        rc = await db.execute(delete(PasswordResetCode).where(PasswordResetCode.expires_at < grace))
        ss = await db.execute(
            update(UserSession)
            .where(UserSession.revoked_at.is_(None), UserSession.expires_at < now)
            .values(revoked_at=now, revoked_reason="expired")
        )
    return {
        "refresh_tokens_deleted": rt.rowcount or 0,
        "reset_codes_deleted": rc.rowcount or 0,
        "sessions_expired": ss.rowcount or 0,
    }
