"""
RD Mídia Indoor — Tasks do domínio de campanhas (W-CAMP).

- ``finalize_expired_campaigns``: vencimento SEM tolerância às 23:59:59 do último dia no
  fuso IANA de CADA tela da campanha (P001 §51; P002 #25). Como cada tela tem seu fuso, a
  verificação roda periodicamente (job controlado, não é polling de UI) e só finaliza quando
  a ÚLTIMA tela passou do seu 23:59:59 local. Idempotente (lock por campanha, skip_locked).
- ``generate_auto_renewals``: renovação automática D-N antes do vencimento local (diária; o beat
  roda a cada 6 h para tolerar reinício do beat — a geração é idempotente).
- ``expire_unpaid_orders``: pedidos aguardando pagamento cuja reserva de 2 h venceu →
  libera reservas/cupom e devolve a campanha a RASCUNHO (pagamento tardio continua aceito).
"""
from __future__ import annotations

from celery.utils.log import get_task_logger

from app.workers.celery_app import celery_app
from app.workers.tasks.auth import run_async

logger = get_task_logger(__name__)


async def _drain_notifications() -> None:
    try:
        from app.modules.notifications.service import drain

        await drain()
    except Exception as exc:  # pragma: no cover
        logger.warning("notifications drain failed: %s", type(exc).__name__)


@celery_app.task(name="app.workers.tasks.campaigns.finalize_expired_campaigns", bind=True)
def finalize_expired_campaigns(self) -> dict:
    return run_async(_finalize())


async def _finalize() -> dict:
    from app.core.database import worker_session
    from app.modules.campaigns.service import finalize_expired

    async with worker_session() as db:
        done = await finalize_expired(db)
    await _drain_notifications()
    if done:
        logger.info("finalize_expired_campaigns finalized=%s", len(done))
    return {"finalized": len(done), "campaign_ids": [str(x) for x in done][:50]}


@celery_app.task(name="app.workers.tasks.campaigns.expire_unpaid_orders", bind=True)
def expire_unpaid_orders(self) -> dict:
    return run_async(_expire_orders())


async def _expire_orders() -> dict:
    from app.core.database import worker_session
    from app.modules.campaigns.checkout import expire_overdue_orders

    async with worker_session() as db:
        ids = await expire_overdue_orders(db)
    await _drain_notifications()
    return {"expired": len(ids), "order_ids": [str(x) for x in ids][:50]}


@celery_app.task(name="app.workers.tasks.campaigns.generate_auto_renewals", bind=True)
def generate_auto_renewals(self) -> dict:
    return run_async(_auto_renew())


async def _auto_renew() -> dict:
    """Renovação automática D-N (ETAPA2 #509): gera pedido + PIX e notifica o cliente. Idempotente."""
    from app.core.database import worker_session
    from app.modules.campaigns.renewals import generate_auto_renewals as gen

    async with worker_session() as db:
        out = await gen(db)
    await _drain_notifications()
    if out["created"] or out["failed"]:
        logger.info("auto_renewals created=%s failed=%s", len(out["created"]), len(out["failed"]))
    return {"created": len(out["created"]), "skipped": len(out["skipped"]), "failed": out["failed"][:50]}
