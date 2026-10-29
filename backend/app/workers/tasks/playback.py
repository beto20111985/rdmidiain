"""
RD Mídia Indoor — Tasks Celery do domínio de players / PoP (W-PLAY).

Jobs CONTROLADOS (P001 §70) — nenhum faz polling de banco por tela:
- ``presence_sweep`` (60 s): offline de conexões meio-abertas/processos mortos (sorted set Redis).
- ``presence_flush`` (300 s): flush coalescido de ``last_seen_at`` + alertas de tela offline.
- ``aggregate_playback`` (300 s): agrega PoP bruto em ``playback_daily_stats``.
- ``deliver_financial`` (60 s): entrega PoP válido EXTERNAL ao financeiro (idempotente).
- ``housekeeping`` (300 s): expira comandos e pedidos de pareamento.
- ``purge_playback`` (diário): retenção de brutos (só agregados + apropriados) e telemetria.
- ``publish_sync_required`` (sob demanda): exemplo/uso por outros domínios — publica
  ``sync_required`` a partir de um processo Celery (``bus.publish_sync``).
"""
from __future__ import annotations

import asyncio
from typing import Any, Coroutine

from celery.utils.log import get_task_logger

from app.workers.celery_app import celery_app

logger = get_task_logger(__name__)


def run_async(coro: Coroutine) -> Any:
    async def _wrapped():
        from app.core.redis import close_redis

        try:
            return await coro
        finally:
            try:
                from app.modules.notifications.service import drain

                await drain()
            except Exception:  # serviço opcional
                pass
            await close_redis()

    return asyncio.run(_wrapped())


@celery_app.task(name="app.workers.tasks.playback.presence_sweep", bind=True)
def presence_sweep(self) -> dict:
    return run_async(_presence_sweep())


async def _presence_sweep() -> dict:
    from app.core.database import worker_session
    from app.modules.presence import service as presence

    async with worker_session() as db:
        gone = await presence.sweep(db=db)
    return {"offline": len(gone)}


@celery_app.task(name="app.workers.tasks.playback.presence_flush", bind=True)
def presence_flush(self) -> dict:
    return run_async(_presence_flush())


async def _presence_flush() -> dict:
    from app.core.database import worker_session
    from app.modules.playback.maintenance import offline_alerts, outdated_alerts
    from app.modules.presence import service as presence

    async with worker_session() as db:
        n = await presence.flush_last_seen(db)
    async with worker_session() as db:
        alerts = await offline_alerts(db)
    async with worker_session() as db:
        outdated = await outdated_alerts(db)
    return {"flushed": n, "alerts": alerts, "outdated_alerts": outdated}


@celery_app.task(name="app.workers.tasks.playback.aggregate_playback", bind=True)
def aggregate_playback(self, limit: int = 5000, rounds: int = 20) -> dict:
    return run_async(_aggregate(limit, rounds))


async def _aggregate(limit: int, rounds: int) -> dict:
    from app.core.database import worker_session
    from app.modules.playback.maintenance import aggregate_pending

    total = {"events": 0, "groups": 0}
    for _ in range(max(1, rounds)):
        async with worker_session() as db:
            res = await aggregate_pending(db, limit)
        total["events"] += res["events"]
        total["groups"] += res["groups"]
        if res["events"] < limit:
            break
    return total


@celery_app.task(name="app.workers.tasks.playback.deliver_financial", bind=True)
def deliver_financial(self, limit: int = 500) -> dict:
    return run_async(_deliver_financial(limit))


async def _deliver_financial(limit: int) -> dict:
    from app.core.database import worker_session
    from app.modules.playback.ingest import deliver_pending_financial

    async with worker_session() as db:
        return await deliver_pending_financial(db, limit)


@celery_app.task(name="app.workers.tasks.playback.housekeeping", bind=True)
def housekeeping(self) -> dict:
    return run_async(_housekeeping())


async def _housekeeping() -> dict:
    from app.core.database import worker_session
    from app.modules.playback.commands import expire_commands
    from app.modules.playback.maintenance import expire_pairing_requests

    async with worker_session() as db:
        cmds = await expire_commands(db)
        pr = await expire_pairing_requests(db)
    return {"commands_expired": cmds, **pr}


@celery_app.task(name="app.workers.tasks.playback.purge_playback", bind=True)
def purge_playback(self, rounds: int = 50) -> dict:
    return run_async(_purge(rounds))


async def _purge(rounds: int) -> dict:
    from app.core.database import worker_session
    from app.modules.playback.maintenance import purge_raw_events, purge_telemetry

    raw = 0
    for _ in range(max(1, rounds)):
        async with worker_session() as db:
            n = await purge_raw_events(db)
        raw += n
        if n == 0:
            break
    async with worker_session() as db:
        tel = await purge_telemetry(db)
    from app.modules.playback import syncevents

    async with worker_session() as db:
        sync = await syncevents.purge(db)
    return {"raw_deleted": raw, "telemetry_deleted": tel, "sync_events_deleted": sync}


@celery_app.task(name="app.workers.tasks.playback.publish_sync_required", bind=True)
def publish_sync_required(self, screen_id: str, version: int) -> int:
    """Publica ``sync_required`` de um processo Celery (síncrono: ``bus.publish_sync``)."""
    from app.modules.websocket import bus

    return bus.to_screen_sync(screen_id, "sync_required", {"version": int(version)})
