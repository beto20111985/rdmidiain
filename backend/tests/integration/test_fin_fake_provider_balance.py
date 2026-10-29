"""BUG-QA-02: o provedor FALSO (dev/test) tem saldo coerente com o externo.

``GET /saldo`` do fake = Σ valor líquido (amount − fee do próprio webhook) das cobranças que ELE marcou
como pagas − saques/PIX OUT concluídos (inclusive reembolso pago por PIX OUT) − estornos que ele
informou; saque em processamento fica BLOQUEADO (reserva, total igual); contestação idem. Derivado dos
registros do fake (sem contador solto) ⇒ a conciliação diária fecha sem divergência quando o ledger
acompanha, e acusa quando a movimentação externa não foi contabilizada. O adapter real é coberto por
``test_fin_adapter``/``test_fin_accounting`` (respx) e não muda.
"""
import uuid
from datetime import timedelta

from sqlalchemy import func, select

from app.models.financial import ReconciliationItem, Refund, Withdrawal
from app.modules.financial import approvals, cloudpaypag as cpp, ledger, reconciliation, refunds, withdrawals
from tests.factories import make_establishment, make_org, make_user
from tests.fin_helpers import (  # noqa: F401
    approve_svc,
    balance,
    create_order,
    ctx_for,
    fin_env,
    make_admin_masters,
    media_item,
    pay,
    payer_org,
    scope,
)

TODAY = ledger.today_local()


async def _paid(db, amount, fee):
    adv = await payer_org(db)
    order = await create_order(db, adv, [media_item(uuid.uuid4(), amount, TODAY, TODAY)], amount)
    assert await pay(db, order, fee_cents=fee) == "PROCESSED"
    return adv, order


async def _reconcile(db):
    run = await reconciliation.run_daily(db)
    await db.commit()
    mism = (await db.execute(select(ReconciliationItem).where(ReconciliationItem.kind == "BALANCE_MISMATCH"))).scalars().all()
    return run, mism


async def _fake_balance():
    return await cpp.get_provider().get_balance()


async def test_paid_charge_credits_net_and_daily_reconciliation_has_no_divergence(db, fin_env):
    assert isinstance(cpp.get_provider(), cpp.FakeCloudPayPag)
    adv, order = await _paid(db, 10000, 299)
    b = await _fake_balance()
    assert (b.total_cents, b.available_cents, b.blocked_cents) == (9701, 9701, 0)  # amount − fee do webhook
    # repetição do paid (outro webhook_event_id) e eventos sem pagamento não creditam
    assert await pay(db, order, fee_cents=299) == "IGNORED"
    _adv2, other = await _paid(db, 5000, 150)
    pending = await create_order(db, adv, [media_item(uuid.uuid4(), 777, TODAY, TODAY)], 777)
    assert await pay(db, pending, status="expired") == "PROCESSED"
    b = await _fake_balance()
    assert b.total_cents == 9701 + 4850
    run, mism = await _reconcile(db)
    assert run.status == "COMPLETED" and run.provider_balance_cents == run.internal_cash_cents == 14551
    assert mism == []
    assert await balance(db, "RD:Cash:CloudPayPag") == 14551


async def test_establishment_withdrawal_reserves_then_debits_and_reconciles(db, fin_env, scope):
    await _paid(db, 10000, 299)
    org = await make_org(db, business_roles=["PARTNER"])
    est = await make_establishment(db, org)
    a = await make_user(db, role="admin")
    exp = await ledger.account(db, "RD:Expense:Commission")
    av = await ledger.account(db, "EST:CommissionPayable:Available", owner_id=est.id, org_id=est.org_id)
    await ledger.post(db, kind="TEST_CREDIT", idempotency_key=f"credit:{uuid.uuid4()}", legs=[(exp, 800), (av, -800)],
                      description="comissão de teste")
    from app.core.crypto import encrypt_str
    from app.core.timeutils import utcnow
    from app.models.establishments import Establishment
    from app.models.financial import EstablishmentPixKey

    db.add(EstablishmentPixKey(id=uuid.uuid4(), establishment_id=est.id, key_type="cpf",
                               key_value_enc=encrypt_str("12345678909", purpose="pix_key", aad=f"est:{est.id}"),
                               key_fingerprint="7" * 64, key_masked="***", holder_name="T", holder_document_enc="v1:x",
                               holder_document_masked="***", active_from=utcnow() - timedelta(hours=13), created_by=a.id))
    await db.commit()
    w = await withdrawals.create_establishment_withdrawal(db, await db.get(Establishment, est.id), 800,
                                                          requested_by=a.id, idempotency_key="saque-fake")
    await db.commit()
    assert await withdrawals.execute_withdrawal(w.id, scope=scope) == "PROCESSING"
    b = await _fake_balance()
    assert (b.total_cents, b.blocked_cents, b.available_cents) == (9701, 800, 8901)  # reserva: total igual
    run, mism = await _reconcile(db)
    assert run.provider_balance_cents == run.internal_cash_cents == 9701 and mism == []
    # concluído no provedor ⇒ debita o valor do saque; o sistema aplica pela consulta (caminho real)
    db.expunge_all()
    w = await db.get(Withdrawal, w.id)
    await cpp.get_provider().set_withdrawal_status(w.provider_external_id, "completed")
    assert await withdrawals.refresh_withdrawal(db, w) == "COMPLETED"
    await db.commit()
    b = await _fake_balance()
    assert (b.total_cents, b.blocked_cents) == (9701 - 800, 0)
    run, mism = await _reconcile(db)
    assert run.provider_balance_cents == run.internal_cash_cents == 8901 and mism == []


async def test_refund_pix_out_debits_with_fee_and_external_refund_is_flagged(db, fin_env, scope, mail):
    a, b = await make_admin_masters(db)
    adv, order = await _paid(db, 10000, 299)
    start = TODAY + timedelta(days=3)
    adv2 = await payer_org(db)
    future = await create_order(db, adv2, [media_item(uuid.uuid4(), 5000, start, start)], 5000)
    assert await pay(db, future, fee_cents=150) == "PROCESSED"
    ref = await refunds.request_refund(db, await ctx_for(db, await make_user(db, role="financial")), future.id, None,
                                       "cliente cancelou antes do início")
    await db.commit()
    req = await approvals.create_request(db, await ctx_for(db, a), "REFUND_PIX_OUT",
                                         {"refund_id": str(ref.id), "pix_key": "cliente@ex.com", "pix_key_type": "email"},
                                         "Reembolso por PIX OUT")
    await db.commit()
    await approve_svc(db, b, req, mail)
    w = (await db.execute(select(Withdrawal).where(Withdrawal.kind == "REFUND"))).scalar_one()
    assert await withdrawals.execute_withdrawal(w.id, scope=scope) == "PROCESSING"
    db.expunge_all()
    w = await db.get(Withdrawal, w.id)
    sent = w.sent_cents
    assert sent == 5000 + cpp.FAKE_WITHDRAWAL_FEE_CENTS  # RD arca a taxa: pagador recebe o valor integral
    await cpp.get_provider().set_withdrawal_status(w.provider_external_id, "completed")
    assert await withdrawals.refresh_withdrawal(db, w) == "COMPLETED"
    await db.commit()
    assert (await db.get(Refund, ref.id)).status == "PAID"
    bal = await _fake_balance()
    assert bal.total_cents == 9701 + 4850 - sent
    run, mism = await _reconcile(db)
    assert run.provider_balance_cents == run.internal_cash_cents == 9701 + 4850 - sent and mism == []
    # estorno feito FORA do sistema (webhook refunded do provedor): o fake debita; o ledger não acompanha
    # ⇒ a conciliação acusa (resolução só por ajuste com dupla aprovação)
    assert await pay(db, order, status="refunded", fee_cents=299) == "PROCESSED"
    bal2 = await _fake_balance()
    assert bal2.total_cents == bal.total_cents - 10000
    run2 = await reconciliation.run_daily(db)
    await db.commit()
    item = (await db.execute(select(ReconciliationItem).where(ReconciliationItem.kind == "BALANCE_MISMATCH"))).scalar_one()
    assert (item.status, item.external_amount_cents - item.internal_amount_cents) == ("DIVERGENT", -10000)
    assert (await db.execute(select(func.count()).select_from(ReconciliationItem)
                             .where(ReconciliationItem.kind == "EXTERNAL_REFUND"))).scalar_one() == 1
    assert run2.provider_balance_cents == bal2.total_cents


async def test_disputed_charge_moves_amount_to_blocked(db, fin_env):
    adv, order = await _paid(db, 10000, 299)
    await _paid(db, 20000, 0)
    assert await pay(db, order, status="disputed", fee_cents=299) == "PROCESSED"
    b = await _fake_balance()
    assert (b.total_cents, b.blocked_cents, b.available_cents) == (29701, 10000, 19701)
    run, mism = await _reconcile(db)
    assert mism == []  # total inalterado (contestação vira item próprio, não divergência de saldo)
