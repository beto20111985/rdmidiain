"""W-BIZ — tasks Celery executadas no processo (``.apply()``), com efeitos verificados no
PostgreSQL real: retry de entregas de notificação, expiração de propostas, avanço de
rollouts (respeitando concorrência) e relatório mensal forçado (idempotente)."""
from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

from sqlalchemy import func, select, update

from app.core.database import create_engine, session_factory
from app.core.timeutils import utcnow
from app.workers.tasks.maintenance import advance_rollouts, expire_crm_proposals
from app.workers.tasks.notifications import retry_pending_deliveries
from app.workers.tasks.reports import monthly_reports


async def _run(fn):
    eng = create_engine(nullpool=True)
    try:
        async with session_factory(eng)() as s:
            out = await fn(s)
            await s.commit()
            return out
    finally:
        await eng.dispose()


def test_retry_pending_deliveries_task_sends_email(clean_state, mail):
    from app.models.notifications import NotificationDelivery
    from app.models.users import User
    from app.modules.notifications import service as svc

    async def seed(s):
        u = User(id=uuid.uuid4(), email=f"w{uuid.uuid4().hex[:6]}@example.com", full_name="W", hashed_password="x")
        s.add(u)
        await s.flush()
        res = await svc.notify(s, [u.id], "campaign.started", {"campaign_name": "X", "campaign_id": "1"},
                               channels=["EMAIL"])
        s.sync_session.info.pop("rd_notify_pending", None)  # simula processo que caiu antes do envio pós-commit
        return u.email, res.created[0]

    # sem loop pós-commit (simula processo que morreu): a entrega fica PENDING
    email, nid = asyncio.run(_run(seed))
    asyncio.run(_run(lambda s: s.execute(update(NotificationDelivery).values(next_attempt_at=utcnow() - timedelta(seconds=1)))))
    stats = retry_pending_deliveries.apply().get(timeout=60)
    assert stats["sent"] >= 1
    assert mail.last_to(email) is not None
    st = asyncio.run(_run(lambda s: s.execute(select(NotificationDelivery.status).where(
        NotificationDelivery.notification_id == nid))))
    assert st.scalar_one() == "SENT"


def test_expire_crm_proposals_task(clean_state):
    from app.models.crm import Proposal
    from app.models.users import User

    async def seed(s):
        u = User(id=uuid.uuid4(), email=f"s{uuid.uuid4().hex[:6]}@example.com", full_name="S", hashed_password="x")
        s.add(u)
        await s.flush()
        p = Proposal(id=uuid.uuid4(), number=f"PROP-T-{uuid.uuid4().hex[:6]}", seller_id=u.id, title="t",
                     status="ENVIADA", subtotal_cents=100, discount_bps=0, discount_cents=0, total_cents=100,
                     validity_days=30, expires_at=utcnow() - timedelta(hours=1), version=1)
        s.add(p)
        return p.id

    pid = asyncio.run(_run(seed))
    assert expire_crm_proposals.apply().get(timeout=60) == {"expired": 1}
    st = asyncio.run(_run(lambda s: s.execute(select(Proposal.status).where(Proposal.id == pid))))
    assert st.scalar_one() == "EXPIRADA"


def test_advance_rollouts_task_respects_concurrency(clean_state):
    from app.models.control_plane import CpInstance, CpRelease, CpRollout, CpRolloutTarget

    async def seed(s):
        rel = CpRelease(id=uuid.uuid4(), version="5.0.0", status="PUBLICADA")
        s.add(rel)
        ro = CpRollout(id=uuid.uuid4(), release_id=rel.id, target_kind="TODAS", max_concurrency=2,
                       failure_policy="PARAR", executor="SIMULACAO_LOCAL", status="EM_ANDAMENTO")
        s.add(ro)
        for i in range(4):
            inst = CpInstance(id=uuid.uuid4(), slug=f"i{i}-{uuid.uuid4().hex[:4]}", name="i", kind="WHITE_LABEL",
                              status="ATIVA", health_status="OK", current_version="4.0.0")
            s.add(inst)
            await s.flush()
            s.add(CpRolloutTarget(rollout_id=ro.id, instance_id=inst.id, position=i, from_version="4.0.0",
                                  to_version="5.0.0", status="NA_FILA"))
        return ro.id

    rid = asyncio.run(_run(seed))
    out = advance_rollouts.apply().get(timeout=60)
    assert out["started"] == 2
    again = advance_rollouts.apply().get(timeout=60)
    assert again["started"] == 0  # limite de concorrência: nada novo enquanto 2 executam
    n = asyncio.run(_run(lambda s: s.execute(select(func.count()).select_from(CpRolloutTarget).where(
        CpRolloutTarget.rollout_id == rid, CpRolloutTarget.status == "EM_EXECUCAO"))))
    assert n.scalar_one() == 2


def test_monthly_reports_task_skips_when_not_first_day_and_force_is_idempotent(clean_state, mail):
    from datetime import date

    from app.core.timeutils import local_date

    today = local_date(utcnow(), "America/Sao_Paulo")
    out = monthly_reports.apply().get(timeout=120)
    if today.day != 1:
        assert out == {"skipped": "not_first_day"}
    forced = monthly_reports.apply(kwargs={"force_period_start": date(2026, 1, 1).isoformat()}).get(timeout=120)
    assert forced["emails"] == 0 and forced["advertisers"] == 0  # sem PoP no período → ninguém recebe
