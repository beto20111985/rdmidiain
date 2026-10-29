"""Tasks Celery do financeiro executadas NO PROCESSO (sem broker), várias vezes seguidas
(loop novo por execução): webhook em background, fechamento/liberação de comissão,
varredura de PIX OUT travado ⇒ UNKNOWN e ausência de retry automático."""
import asyncio
import uuid
from datetime import timedelta


from app.core.database import create_engine, session_factory
from app.core.timeutils import utcnow
from app.models.financial import CommissionCompetenceRun, PaymentOrder, WebhookEvent, Withdrawal
from app.modules.financial import commission, ledger, payments
from app.workers.tasks import financial as T
from tests.fin_helpers import create_order, fin_env, make_screen, media_item, payer_org, plan_provider, pop, webhook_for  # noqa: F401


def run(coro_fn):
    async def _wrap():
        eng = create_engine(nullpool=True)
        try:
            async with session_factory(eng)() as db:
                return await coro_fn(db)
        finally:
            await eng.dispose()

    return asyncio.run(_wrap())


def test_webhook_processed_in_background_task_idempotently(fin_env):
    async def setup(db):
        org = await payer_org(db)
        o = await create_order(db, org, [media_item(uuid.uuid4(), 4000, ledger.today_local(), ledger.today_local())], 4000)
        raw, headers = webhook_for(o, event_id=31337)
        res = await payments.ingest_webhook(db, raw, headers)
        await db.commit()
        return o.id, res.event_id

    oid, ev_id = run(setup)
    assert T.process_webhook_event.apply(args=[str(ev_id)]).get(timeout=60) == "PROCESSED"
    assert T.process_webhook_event.apply(args=[str(ev_id)]).get(timeout=60) == "PROCESSED"  # idempotente

    async def check(db):
        o = await db.get(PaymentOrder, oid)
        ev = await db.get(WebhookEvent, ev_id)
        return o.status, ev.status, ev.attempts

    assert run(check) == ("PAID", "PROCESSED", 1)
    assert T.process_pending_webhooks.apply().get(timeout=60) == {"processed": 0}


def test_close_commissions_task_closes_previous_month_and_releases(fin_env):
    prev_end = ledger.month_start(ledger.today_local()) - timedelta(days=1)

    async def setup(db):
        from tests.factories import make_establishment, make_org

        adv = await payer_org(db)
        cid = uuid.uuid4()
        o = await create_order(db, adv, [media_item(cid, 5000, prev_end, prev_end)], 5000)
        raw, headers = webhook_for(o, event_id=999)
        res = await payments.ingest_webhook(db, raw, headers)
        await db.commit()
        await payments.process_webhook_event(db, res.event_id)
        await db.commit()
        est = await make_establishment(db, await make_org(db, business_roles=["PARTNER"]))
        scr = await make_screen(db, est)
        plan_provider({cid: {scr.id: {prev_end: 10}}})
        await commission.accrue_commission_for_events(db, pop(cid, scr.id, prev_end, 10))
        await db.commit()
        return est.id

    est_id = run(setup)
    r1 = T.close_commissions.apply().get(timeout=120)
    r2 = T.close_commissions.apply().get(timeout=120)
    assert r1["closed"] and r1["closed"][0]["commission_cents"] == 250 and r2["closed"] == []

    async def check(db):
        run_row = await db.get(CommissionCompetenceRun, ledger.month_start(prev_end))
        return run_row.status, await ledger.balance_of(db, "EST:CommissionPayable:Available", est_id)

    assert run(check) == ("RELEASED", 250)


def test_sweep_marks_stuck_executing_withdrawal_unknown_and_tasks_never_retry(fin_env):
    async def setup(db):
        w = Withdrawal(id=uuid.uuid4(), kind="RD", origin="MANUAL", pix_key_type="cpf", destination_enc="v1:x",
                       destination_masked="***", amount_cents=1000, status="EXECUTING",
                       approval_request_id=await _approval(db), idempotency_key=uuid.uuid4().hex,
                       executing_at=utcnow() - timedelta(minutes=30))
        db.add(w)
        await db.commit()
        return w.id

    wid = run(setup)
    out = T.sweep_financial.apply().get(timeout=60)
    assert out["stuck_to_unknown"] == 1

    async def check(db):
        return (await db.get(Withdrawal, wid)).status

    assert run(check) == "UNKNOWN"
    for task in (T.execute_withdrawal, T.process_webhook_event, T.close_commissions, T.reconcile_daily):
        assert task.max_retries == 0


async def _approval(db):
    """Pedido DUAL já aprovado por OUTRO Master ativo (o guard de INSERT do banco exige modo
    inicializado, Masters ativos e aprovador ≠ solicitante — AÇÃO 003)."""
    from app.models.financial import FinancialApprovalRequest
    from app.modules.financial import common
    from tests.fin_helpers import make_admin_masters

    u, v = await make_admin_masters(db)
    r = FinancialApprovalRequest(id=uuid.uuid4(), action_type="RD_WITHDRAWAL", payload={"x": 1},
                                 payload_sha256=common.canonical_sha256({"x": 1}), amount_cents=1000, reason="teste",
                                 requested_by=u.id, status="APPROVED", approved_by=v.id, approved_at=utcnow(),
                                 expires_at=utcnow() + timedelta(hours=1), execution_state="EXECUTING",
                                 idempotency_key=uuid.uuid4().hex, approval_mode="DUAL")
    db.add(r)
    await db.flush()
    return r.id
