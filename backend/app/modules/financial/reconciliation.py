"""
RD Mídia Indoor — Conciliação CloudPayPag × ledger (ADR_009 itens 1 e 9; A5 D8; P001 §78, §82).

- Diária: ``GET /saldo`` (saldo_total) × saldo de ``RD:Cash:CloudPayPag`` (+ bloqueado
  via API); saques PROCESSING/UNKNOWN × ``GET /withdrawals/{external_id}``.
- Cobranças: a API pública NÃO tem consulta/listagem de cobranças ⇒ a conciliação de
  cobranças é feita pelos webhooks (divergências viram itens aqui).
- Itens ``DIVERGENT`` só são resolvidos por **ajuste aprovado** (dupla aprovação,
  ação ``RECONCILIATION_ADJUSTMENT``). Nunca ajuste silencioso.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.errors import Conflict, NotFound
from app.core.timeutils import utcnow
from app.models.financial import ReconciliationItem, ReconciliationRun, Withdrawal, WithdrawalStatus as WS
from app.modules.financial import cloudpaypag as cpp
from app.modules.financial import common, ledger

log = structlog.get_logger("rd.financial.reconciliation")


async def open_item(db: AsyncSession, kind: str, dedupe_key: str, *, run_id: Any = None, internal_ref: Optional[str] = None,
                    external_ref: Optional[str] = None, internal_amount_cents: Optional[int] = None,
                    external_amount_cents: Optional[int] = None, detail: Optional[dict] = None) -> Optional[uuid.UUID]:
    """Abre item DIVERGENT (um aberto por (tipo, chave)); devolve o id novo ou None se já aberto."""
    tbl = ReconciliationItem.__table__
    row = (await db.execute(
        pg_insert(tbl).values(
            id=uuid.uuid4(), run_id=run_id, kind=kind[:40], dedupe_key=dedupe_key[:200], internal_ref=internal_ref,
            external_ref=external_ref, internal_amount_cents=internal_amount_cents,
            external_amount_cents=external_amount_cents, detail=detail or {}, status="DIVERGENT",
        ).on_conflict_do_nothing(index_elements=["kind", "dedupe_key"], index_where=tbl.c.status == "DIVERGENT")
        .returning(tbl.c.id)
    )).first()
    if row is not None:
        log.warning("reconciliation_item_opened", kind=kind)
        try:
            from app.modules.notifications.service import StaffRecipients

            await common.notify(db, [StaffRecipients("finance.reconcile")], "finance.reconciliation_divergent",
                                {"message": f"{kind}: {internal_ref or external_ref or dedupe_key}"[:300],
                                 "item_id": str(row[0])}, dedupe_key=f"recon:{row[0]}")
        except Exception as exc:  # a divergência está gravada; notificação é complementar
            log.error("reconciliation_notify_failed", error_type=type(exc).__name__)
        return row[0]
    return None


def item_public(i: ReconciliationItem) -> dict:
    return {
        "id": str(i.id), "run_id": str(i.run_id) if i.run_id else None, "kind": i.kind, "status": i.status,
        "internal_ref": i.internal_ref, "external_ref": i.external_ref,
        "internal_amount_cents": i.internal_amount_cents, "external_amount_cents": i.external_amount_cents,
        "detail": i.detail, "created_at": i.created_at, "resolved_at": i.resolved_at,
        "resolution_approval_id": str(i.resolution_approval_id) if i.resolution_approval_id else None,
        "resolution_transaction_id": str(i.resolution_transaction_id) if i.resolution_transaction_id else None,
    }


async def run_daily(db: AsyncSession, *, created_by: Any = None, kind: str = "DAILY") -> ReconciliationRun:
    run = ReconciliationRun(id=uuid.uuid4(), kind=kind, status="RUNNING", created_by=created_by, summary={})
    db.add(run)
    await db.flush()
    provider = cpp.get_provider()
    summary: dict = {"withdrawals_checked": 0, "withdrawals_updated": 0, "items_opened": 0}
    try:
        bal = await provider.get_balance()
        blocks = await provider.get_api_blocks()
    except cpp.ProviderError as exc:
        run.status = "PROVIDER_UNAVAILABLE"
        run.summary = {"error": exc.code}
        run.finished_at = utcnow()
        await db.flush()
        return run
    internal = await ledger.balance_of(db, "RD:Cash:CloudPayPag") + await ledger.balance_of(db, "RD:Cash:CloudPayPag:Blocked")
    run.provider_balance_cents = bal.total_cents
    run.internal_cash_cents = internal
    summary["provider"] = {"available_cents": bal.available_cents, "blocked_cents": bal.blocked_cents,
                           "total_cents": bal.total_cents, "api_blocked_cents": blocks.api_blocked_cents}
    if bal.total_cents != internal:
        if await open_item(db, "BALANCE_MISMATCH", f"balance:{ledger.today_local().isoformat()}", run_id=run.id,
                           internal_ref="RD:Cash:CloudPayPag", internal_amount_cents=internal,
                           external_amount_cents=bal.total_cents,
                           detail={"difference_cents": bal.total_cents - internal}):
            summary["items_opened"] += 1
    from app.modules.financial import withdrawals

    rows = (await db.execute(
        select(Withdrawal).where(Withdrawal.status.in_((WS.PROCESSING.value, WS.UNKNOWN.value)),
                                 Withdrawal.provider_external_id.isnot(None)).with_for_update(skip_locked=True)
    )).scalars().all()
    for w in rows:
        summary["withdrawals_checked"] += 1
        before = w.status
        try:
            after = await withdrawals.refresh_withdrawal(db, w)
        except cpp.ProviderError as exc:
            summary.setdefault("errors", []).append({"withdrawal_id": str(w.id), "error": exc.code})
            continue
        if after != before:
            summary["withdrawals_updated"] += 1
    run.summary = summary
    run.status = "COMPLETED"
    run.finished_at = utcnow()
    await db.flush()
    await audit(db, None, "reconciliation.run", "reconciliation_run", run.id, origin="worker",
                after={"status": run.status, "provider_total_cents": bal.total_cents, "internal_cents": internal})
    return run


async def resolve_with_approval(db: AsyncSession, item_id: Any, approval: Any, payload: dict, approver_id: Any) -> dict:
    """Executa a resolução APROVADA (dupla aprovação): ajuste no ledger e/ou desfecho do saque."""
    from app.modules.financial import approvals as ap
    from app.modules.financial import withdrawals

    item = await db.get(ReconciliationItem, common.as_uuid(item_id), with_for_update=True)
    if item is None:
        raise NotFound()
    if item.status != "DIVERGENT":
        raise Conflict("RECONCILIATION_ITEM_RESOLVED", "Item já resolvido.")
    tx_id = None
    if payload.get("legs"):
        res = await ap.post_manual_legs(db, payload["legs"], kind="RECONCILIATION_ADJUSTMENT",
                                        key=f"recon:{item.id}:{approval.id}",
                                        description=f"Ajuste de conciliação {item.kind}: {approval.reason}"[:500],
                                        approval=approval, approver_id=approver_id,
                                        competence_date=payload.get("competence_date"))
        tx_id = res.transaction_id
    wr = payload.get("withdrawal_resolution")
    if wr:
        w = await db.get(Withdrawal, common.as_uuid(wr["withdrawal_id"]), with_for_update=True)
        if w is None:
            raise NotFound()
        if w.status != WS.UNKNOWN.value:
            raise Conflict("WITHDRAWAL_NOT_UNKNOWN", "Saque não está em estado desconhecido.")
        if wr["outcome"] == "COMPLETED":
            if wr.get("external_id"):
                w.provider_external_id = str(wr["external_id"])[:80]
            if wr.get("fee_cents") is not None:
                w.fee_cents = int(wr["fee_cents"])
            w.status = WS.PROCESSING.value
            await db.flush()
            await withdrawals.settle(db, w)
        else:
            w.status = WS.PROCESSING.value
            await db.flush()
            await withdrawals.fail(db, w, "confirmado não executado (conciliação aprovada)")
    item.status = "RESOLVED"
    item.resolution_approval_id = approval.id
    item.resolution_transaction_id = tx_id
    item.resolved_by = approver_id
    item.resolved_at = utcnow()
    await db.flush()
    return {"item_id": str(item.id), "transaction_id": str(tx_id) if tx_id else None}
