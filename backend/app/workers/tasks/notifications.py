"""RD Mídia Indoor — Celery: notificações (AÇÃO 002 / W-BIZ).

- ``retry_pending_deliveries`` (beat, 2 min): retry CONTROLADO de entregas que não
  foram concluídas no pós-commit (processo caiu, worker encerrou o loop, SMTP fora)
  — com backoff por entrega e reivindicação ``FOR UPDATE SKIP LOCKED``. Não é polling
  de interface: a central é atualizada por evento (``notification.created``).
- ``purge_old_notifications`` (beat, diário): remove notificações LIDAS com mais de
  180 dias (a central não cresce sem limite; entregas caem em cascata).
"""
from __future__ import annotations

from datetime import timedelta

from celery.utils.log import get_task_logger

from app.workers.celery_app import celery_app
from app.workers.tasks.auth import run_async

logger = get_task_logger(__name__)

READ_RETENTION_DAYS = 180


@celery_app.task(name="app.workers.tasks.notifications.retry_pending_deliveries", bind=True)
def retry_pending_deliveries(self) -> dict:
    return run_async(_retry_pending())


async def _retry_pending() -> dict:
    from app.core.database import create_engine
    from app.modules.notifications.service import dispatch_deliveries

    eng = create_engine(nullpool=True)
    try:
        stats = await dispatch_deliveries(None, engine=eng)
    finally:
        await eng.dispose()
    logger.info("retry_pending_deliveries %s", stats)
    return stats


@celery_app.task(name="app.workers.tasks.notifications.purge_old_notifications", bind=True)
def purge_old_notifications(self) -> dict:
    return run_async(_purge_old())


async def _purge_old() -> dict:
    from sqlalchemy import delete

    from app.core.database import worker_session
    from app.core.timeutils import utcnow
    from app.models.notifications import Notification

    cutoff = utcnow() - timedelta(days=READ_RETENTION_DAYS)
    async with worker_session() as db:
        res = await db.execute(delete(Notification).where(Notification.read_at.is_not(None), Notification.read_at < cutoff))
    return {"deleted": int(res.rowcount or 0)}
