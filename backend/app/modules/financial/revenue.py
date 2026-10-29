"""
RD Mídia Indoor — Reconhecimento diário de receita de mídia (ETAPA2 #1289–#1293; A5 D1.5).

Para cada cronograma ``revenue_schedules`` ATIVO (cobrança paga × campanha):
``alvo(t) = floor(R_liq · dias_decorridos(t) / dias_totais)``; o último dia leva o
restante (alvo = R_liq). Lançamento = alvo − já reconhecido (derivado do ledger):
D ``ADV:DeferredRevenue`` / C ``RD:Revenue:Media``. Idempotente por
(cronograma, dia, alvo). Dia = data local America/Sao_Paulo (competência financeira).
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.financial import LedgerAccount, LedgerPosting, LedgerTransaction, PaymentOrder, RevenueSchedule
from app.modules.financial import ledger

KIND = "REVENUE_RECOGNITION"


async def recognized_cents(db: AsyncSession, schedule_id: uuid.UUID) -> int:
    """Σ reconhecido (débitos na receita diferida pelos lançamentos deste cronograma)."""
    q = (
        select(func.coalesce(func.sum(LedgerPosting.amount_cents), 0))
        .join(LedgerTransaction, LedgerTransaction.id == LedgerPosting.transaction_id)
        .join(LedgerAccount, LedgerAccount.id == LedgerPosting.account_id)
        .where(LedgerTransaction.ref_type == "revenue_schedule", LedgerTransaction.ref_id == str(schedule_id),
               LedgerTransaction.kind == KIND, LedgerAccount.code == "ADV:DeferredRevenue")
    )
    return int((await db.execute(q)).scalar_one())


def target_through(eligible_net: int, start: date, end: date, through: date) -> int:
    total = (end - start).days + 1
    elapsed = min(max((through - start).days + 1, 0), total)
    if elapsed >= total:
        return eligible_net
    return (eligible_net * elapsed) // total


async def recognize_schedule(db: AsyncSession, sch: RevenueSchedule, through: date) -> int:
    if sch.status != "ACTIVE":
        return 0
    net = sch.eligible_cents - sch.refunded_cents
    target = target_through(net, sch.start_date, sch.end_date, through)
    done = await recognized_cents(db, sch.id)
    delta = target - done
    posted = 0
    if delta > 0:
        deferred = await ledger.account(db, "ADV:DeferredRevenue", owner_id=sch.org_id, org_id=sch.org_id)
        media = await ledger.account(db, "RD:Revenue:Media")
        day = min(through, sch.end_date)
        await ledger.post(
            db, kind=KIND, idempotency_key=f"rev:{sch.id}:{day.isoformat()}:{target}",
            legs=[(deferred, delta), (media, -delta)],
            description=f"Reconhecimento de receita de mídia — campanha {sch.campaign_id}", competence_date=day,
            ref_type="revenue_schedule", ref_id=sch.id, org_id=sch.org_id,
            meta={"campaign_id": str(sch.campaign_id), "through": day.isoformat(), "target_cents": target},
        )
        posted = delta
    if target >= net and through >= sch.end_date:
        sch.status = "COMPLETED"
    return posted


async def recognize_revenue(db: AsyncSession, *, through: Optional[date] = None) -> dict:
    """Reconhece até ``through`` (padrão: ontem, dia local SP — dias completos)."""
    through = through or (ledger.today_local() - timedelta(days=1))
    rows = (await db.execute(
        select(RevenueSchedule).join(PaymentOrder, PaymentOrder.id == RevenueSchedule.payment_order_id)
        .where(RevenueSchedule.status == "ACTIVE", RevenueSchedule.start_date <= through,
               PaymentOrder.status.in_(("PAID", "PARTIALLY_REFUNDED")))
        .order_by(RevenueSchedule.start_date).with_for_update(of=RevenueSchedule, skip_locked=True)
    )).scalars().all()
    total = 0
    for sch in rows:
        total += await recognize_schedule(db, sch, through)
    return {"through": through.isoformat(), "schedules": len(rows), "recognized_cents": total}


async def recognized_by_period(db: AsyncSession, start: date, end: date, *, org_id=None) -> int:
    """Receita de mídia reconhecida no período (para relatórios/dashboards)."""
    q = (
        select(func.coalesce(func.sum(-LedgerPosting.amount_cents), 0))
        .join(LedgerAccount, LedgerAccount.id == LedgerPosting.account_id)
        .join(LedgerTransaction, LedgerTransaction.id == LedgerPosting.transaction_id)
        .where(LedgerAccount.code == "RD:Revenue:Media", LedgerPosting.competence_date.between(start, end))
    )
    if org_id is not None:
        q = q.where(LedgerTransaction.org_id == org_id)
    return int((await db.execute(q)).scalar_one())
