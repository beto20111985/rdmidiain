"""RD Mídia Indoor — Celery: relatórios (AÇÃO 002 / W-BIZ).

- ``monthly_reports`` (beat, a cada hora): no dia 1 (America/Sao_Paulo) gera e envia por
  e-mail o relatório MENSAL do mês anterior a anunciantes e estabelecimentos com exibições
  válidas. Idempotente (``report_runs``): rodar várias vezes no dia não duplica envios.
- ``purge_expired_exports`` (beat, diário): apaga do storage os arquivos exportados vencidos.
"""
from __future__ import annotations

from typing import Optional

from celery.utils.log import get_task_logger

from app.workers.celery_app import celery_app
from app.workers.tasks.auth import run_async

logger = get_task_logger(__name__)


@celery_app.task(name="app.workers.tasks.reports.monthly_reports", bind=True)
def monthly_reports(self, force_period_start: Optional[str] = None) -> dict:
    return run_async(_monthly(force_period_start))


async def _monthly(force_period_start: Optional[str]) -> dict:
    from datetime import date, timedelta

    from app.core.database import worker_session
    from app.core.timeutils import DEFAULT_TIMEZONE, local_date, utcnow
    from app.modules.notifications.service import drain
    from app.modules.reports import data as rdata
    from app.modules.reports.service import previous_month, run_monthly

    today = local_date(utcnow(), DEFAULT_TIMEZONE)
    if force_period_start:  # "AAAA-MM-01": mês a (re)enviar — idempotente por report_runs
        start = date.fromisoformat(force_period_start).replace(day=1)
        period = previous_month((start.replace(day=28) + timedelta(days=4)).replace(day=1))
    elif today.day != 1:
        return {"skipped": "not_first_day"}
    else:
        period = previous_month(today)
    async with worker_session() as db:
        stats = await run_monthly(db, rdata.Period(period.start, period.end))
    await drain()
    logger.info("monthly_reports %s", stats)
    return stats


@celery_app.task(name="app.workers.tasks.reports.purge_expired_exports", bind=True)
def purge_expired_exports(self) -> dict:
    return run_async(_purge())


async def _purge() -> dict:
    from sqlalchemy import select, update

    from app.core.database import worker_session
    from app.core.storage import get_storage
    from app.core.timeutils import utcnow
    from app.models.reports import ReportExport

    removed = 0
    async with worker_session() as db:
        rows = list((await db.execute(select(ReportExport).where(
            ReportExport.expires_at < utcnow(), ReportExport.storage_key.is_not(None)).limit(500))).scalars())
        for e in rows:
            await get_storage().delete(e.storage_key)
            removed += 1
        if rows:
            await db.execute(update(ReportExport).where(ReportExport.id.in_([e.id for e in rows]))
                             .values(storage_key=None))
    return {"removed": removed}
