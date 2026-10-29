"""
RD Mídia Indoor — Tasks do inventário/scheduler (W-INV; ADR_006).

- ``sweep_inventory_holds`` (beat 60 s): expira reservas vencidas (set-based,
  idempotente, ``SKIP LOCKED``) — a expiração LAZY já protege a venda entre execuções.
- ``regenerate_stale_manifests`` (beat 15 min): regeneração DIÁRIA por tela — gera nova
  versão para toda tela cujo manifesto começa antes da meia-noite local de hoje
  (fusos diferentes viram o dia em horas diferentes) e publica ``screen.sync``.
- ``regenerate_manifests(screen_ids, reason)``: invalidação de lotes grandes enfileirada
  após o commit por ``invalidate_screens``.
- ``prune_manifest_versions`` (diária): poda conteúdo antigo (mantém hashes/listas).

O vencimento de campanhas (23:59:59 local, FINALIZADA) é agendado pelo W-CAMP
(``campaigns.service.finalize_expired``) — removido daqui para não haver duplicidade.

Removidas (AÇÃO 002): ``expire_campaigns`` (W-CAMP), ``expire_reservations`` (slots de relógio), ``generate_daily_slots``
(``InventorySlot`` — modelo substituído), ``generate_manifest`` (gerador paralelo, lista
plana) e ``process_pending_playback`` (HMAC legado; PoP é do W-PLAY).
"""
from __future__ import annotations

import asyncio
from typing import Any, Coroutine, Iterable

from celery.utils.log import get_task_logger

from app.workers.celery_app import celery_app

logger = get_task_logger(__name__)


def run_async(coro: Coroutine) -> Any:
    return asyncio.run(coro)


async def _drain_notifications() -> None:
    try:
        from app.modules.notifications.service import drain

        await drain()
    except Exception as exc:  # pragma: no cover
        logger.warning("notifications drain failed: %s", type(exc).__name__)


@celery_app.task(name="app.workers.tasks.scheduler.sweep_inventory_holds", bind=True)
def sweep_inventory_holds(self) -> dict:
    result = run_async(_sweep())
    if result["expired"]:
        logger.info("sweep_inventory_holds %s", result)
    return result


async def _sweep() -> dict:
    from app.core.database import worker_session
    from app.modules.inventory.booking import sweep_expired_holds

    async with worker_session() as db:
        expired = await sweep_expired_holds(db)
    await _drain_notifications()
    return {"expired": len(expired), "holds": [e["hold_id"] for e in expired][:50]}


@celery_app.task(name="app.workers.tasks.scheduler.regenerate_stale_manifests", bind=True)
def regenerate_stale_manifests(self, limit: int = 500) -> dict:
    return run_async(_regenerate_stale(limit))


async def _regenerate_stale(limit: int) -> dict:
    from app.core.database import worker_session
    from app.modules.inventory.manifests import stale_screens_sql

    async with worker_session() as db:
        ids = [str(r[0]) for r in (await db.execute(stale_screens_sql(), {"limit": int(limit)})).all()]
    res = await _regenerate(ids, "daily")
    return {"screens": len(ids), **res}


async def _regenerate(screen_ids: Iterable[str], reason: str) -> dict:
    """Uma transação por tela (falha isolada; lock curto)."""
    from app.core.database import worker_session
    from app.modules.inventory.manifests import invalidate_screens

    done, failed = 0, 0
    for sid in screen_ids:
        try:
            async with worker_session() as db:
                await invalidate_screens(db, [sid], reason)
            done += 1
        except Exception as exc:
            failed += 1
            logger.error("manifest regeneration failed screen=%s err=%s", sid, type(exc).__name__)
    await _drain_notifications()
    return {"generated": done, "failed": failed}


@celery_app.task(name="app.workers.tasks.scheduler.regenerate_manifests", bind=True, max_retries=3)
def regenerate_manifests(self, screen_ids: list, reason: str = "invalidate") -> dict:
    return run_async(_regenerate(list(screen_ids), reason))


@celery_app.task(name="app.workers.tasks.scheduler.prune_manifest_versions", bind=True)
def prune_manifest_versions(self) -> dict:
    return run_async(_prune())


async def _prune() -> dict:
    from app.core.database import worker_session
    from app.modules.inventory.manifests import prune_manifest_content

    async with worker_session() as db:
        n = await prune_manifest_content(db)
    return {"pruned": n}
