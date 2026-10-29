"""
RD Mídia Indoor — Tasks do domínio financeiro (W-FIN; ADR_009). Fila ``financial``.

Modelo: ``run_async`` + ``worker_session`` (engine NullPool por execução). NENHUMA task
de PIX OUT tem retry automático (o provedor não oferece idempotência em
``POST /withdrawals``): ``max_retries=0`` e máquina de estados (EXECUTING gravado antes
da chamada; reentrega da mensagem encontra EXECUTING e não repete).

- ``process_webhook_event``      — processa um webhook persistido (rota pública só grava).
- ``process_pending_webhooks``   — recuperação: RECEIVED/FAILED antigos (broker indisponível etc.).
- ``recognize_revenue_daily``    — reconhecimento diário de receita (idempotente; horário).
- ``accrue_commission_daily``    — rede de segurança: pede ao W-PLAY a entrega dos PoP pendentes.
- ``close_commissions``          — fecha competências encerradas (23:59:59 SP do último dia),
                                   libera Pending→Available (dia 1) e cria pagamentos automáticos.
- ``execute_withdrawal``         — executa PIX OUT aprovado/solicitado (SEM retry).
- ``refresh_withdrawals``        — consulta saques em processamento (``GET /withdrawals/{id}``).
- ``sweep_financial``            — EXECUTING travado ⇒ UNKNOWN; aprovações expiradas; callbacks falhos.
- ``reconcile_daily``            — conciliação diária ``/saldo`` × ledger.
"""
from __future__ import annotations

from celery.utils.log import get_task_logger

from app.workers.celery_app import celery_app
from app.workers.tasks.auth import run_async

logger = get_task_logger(__name__)

T = "app.workers.tasks.financial."


async def _drain() -> None:
    from app.modules.financial import common

    try:
        from app.modules.notifications import service as notify_service

        await notify_service.drain()
    except Exception:  # pragma: no cover
        pass
    await common.drain()


# ─── Webhooks ────────────────────────────────────────────────────────────────

@celery_app.task(name=T + "process_webhook_event", bind=True, max_retries=0)
def process_webhook_event(self, event_id: str) -> str:
    return run_async(_process_webhook(event_id))


async def _process_webhook(event_id: str) -> str:
    from app.core.database import worker_session
    from app.models.financial import WebhookEvent
    from app.modules.financial import payments

    try:
        async with worker_session() as db:
            status = await payments.process_webhook_event(db, event_id)
    except Exception as exc:
        logger.error("webhook_processing_failed %s %s", event_id, type(exc).__name__)
        from app.modules.financial.common import as_uuid

        async with worker_session() as db:
            ev = await db.get(WebhookEvent, as_uuid(event_id), with_for_update=True)
            if ev is not None and ev.status in ("RECEIVED", "PROCESSING"):
                ev.status = "FAILED"
                ev.attempts = (ev.attempts or 0) + 1
                ev.last_error = f"{type(exc).__name__}: {getattr(exc, 'code', '')}"[:500]
        status = "FAILED"
    await _drain()
    return status


@celery_app.task(name=T + "process_pending_webhooks", bind=True, max_retries=0)
def process_pending_webhooks(self) -> dict:
    return run_async(_pending_webhooks())


async def _pending_webhooks() -> dict:
    from app.core.database import worker_session
    from app.modules.financial import payments

    async with worker_session() as db:
        ids = await payments.pending_webhook_ids(db)
    results = {}
    for i in ids:
        results[str(i)] = await _process_webhook(str(i))
    return {"processed": len(ids)}


# ─── Receita / comissão ──────────────────────────────────────────────────────

@celery_app.task(name=T + "recognize_revenue_daily", bind=True, max_retries=0)
def recognize_revenue_daily(self) -> dict:
    return run_async(_recognize())


async def _recognize() -> dict:
    from app.core.database import worker_session
    from app.modules.financial import revenue

    async with worker_session() as db:
        return await revenue.recognize_revenue(db)


@celery_app.task(name=T + "accrue_commission_daily", bind=True, max_retries=0)
def accrue_commission_daily(self) -> dict:
    return run_async(_accrue_daily())


async def _accrue_daily() -> dict:
    from app.core.database import worker_session

    try:
        from app.modules.playback.service import deliver_pending_financial
    except ImportError:
        return {"delivered": 0, "reason": "PLAYBACK_SERVICE_UNAVAILABLE"}
    total = 0
    for _ in range(50):
        async with worker_session() as db:
            r = await deliver_pending_financial(db, 500)
        total += int(r.get("delivered", 0))
        if not r.get("delivered"):
            break
    return {"delivered": total}


@celery_app.task(name=T + "close_commissions", bind=True, max_retries=0)
def close_commissions(self) -> dict:
    return run_async(_close_commissions())


async def _close_commissions() -> dict:
    from app.core.database import worker_session
    from app.modules.financial import commission, withdrawals

    out = []
    async with worker_session() as db:
        due = await commission.due_competences(db)
    for comp in due:
        async with worker_session() as db:
            run = await commission.close_competence(db, comp)
        async with worker_session() as db:
            rel = await commission.release_competence(db, comp)
        out.append({"competence": comp.isoformat(), "commission_cents": run.commission_cents,
                    "residue_cents": run.residue_cents, "released": rel["released"]})
    auto = None
    if out:
        async with worker_session() as db:
            auto = await withdrawals.auto_payouts(db)
    await _drain()
    return {"closed": out, "auto_payouts": auto}


# ─── PIX OUT ─────────────────────────────────────────────────────────────────

@celery_app.task(name=T + "execute_withdrawal", bind=True, max_retries=0, acks_late=True)
def execute_withdrawal(self, withdrawal_id: str) -> str:
    """SEM retry automático: o estado EXECUTING impede reexecução (reentrega da mensagem é inócua)."""
    return run_async(_execute_withdrawal(withdrawal_id))


async def _execute_withdrawal(withdrawal_id: str) -> str:
    from app.modules.financial import withdrawals

    status = await withdrawals.execute_withdrawal(withdrawal_id)
    await _drain()
    return status


@celery_app.task(name=T + "refresh_withdrawals", bind=True, max_retries=0)
def refresh_withdrawals(self) -> dict:
    return run_async(_refresh_withdrawals())


async def _refresh_withdrawals() -> dict:
    from sqlalchemy import select

    from app.core.database import worker_session
    from app.models.financial import Withdrawal
    from app.modules.financial import cloudpaypag as cpp
    from app.modules.financial import withdrawals

    async with worker_session() as db:
        ids = [r[0] for r in (await db.execute(select(Withdrawal.id).where(
            Withdrawal.status.in_(("PROCESSING", "UNKNOWN")), Withdrawal.provider_external_id.isnot(None)).limit(200))).all()]
    changed = 0
    for wid in ids:
        try:
            async with worker_session() as db:
                w = await db.get(Withdrawal, wid, with_for_update=True)
                before = w.status
                if await withdrawals.refresh_withdrawal(db, w) != before:
                    changed += 1
        except cpp.ProviderError as exc:
            logger.warning("refresh_withdrawal_failed %s %s", wid, exc.code)
    await _drain()
    return {"checked": len(ids), "changed": changed}


# ─── Varreduras / conciliação ────────────────────────────────────────────────

@celery_app.task(name=T + "sweep_financial", bind=True, max_retries=0)
def sweep_financial(self) -> dict:
    return run_async(_sweep())


async def _sweep() -> dict:
    from app.core.database import worker_session
    from app.modules.financial import approvals, payments, withdrawals

    async with worker_session() as db:
        stuck = await withdrawals.sweep_stuck_executing(db)
    async with worker_session() as db:
        expired = await approvals.expire_requests(db)
    async with worker_session() as db:
        callbacks = await payments.retry_failed_callbacks(db)
    await _drain()
    return {"stuck_to_unknown": stuck, "approvals_expired": expired, "callbacks_recovered": callbacks}


@celery_app.task(name=T + "reconcile_daily", bind=True, max_retries=0)
def reconcile_daily(self) -> dict:
    return run_async(_reconcile())


async def _reconcile() -> dict:
    from app.core.database import worker_session
    from app.modules.financial import reconciliation

    async with worker_session() as db:
        run = await reconciliation.run_daily(db)
        out = {"run_id": str(run.id), "status": run.status, "summary": run.summary}
    await _drain()
    return out
