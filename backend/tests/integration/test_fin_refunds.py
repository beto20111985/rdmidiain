"""Cancelamento/reembolso (A5 D7; P001 §90): antes do início integral; dia 3 proporcional ao
PoP; dia 8 negado; decisão do admin (dupla aprovação) com estorno de comissão — pendente
revertida, já sacada ⇒ saldo negativo + bloqueio de saque; pagamento por PIX OUT ou
BLOQUEADO POR DEPENDÊNCIA EXTERNA."""
import uuid
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from app.core.errors import DomainError
from app.models.financial import CommissionAccrual, PaymentOrder, Refund, Withdrawal
from app.modules.financial import approvals, cloudpaypag as cpp, commission, ledger, refunds, withdrawals
from tests.factories import make_establishment, make_org, make_user
from tests.fin_helpers import (  # noqa: F401
    balance,
    create_order,
    ctx_for,
    fin_env,
    make_admin_masters,
    make_screen,
    pay,
    payer_org,
    plan_provider,
    pop,
    provider_config,
    scope,
)

TODAY = ledger.today_local()


async def campaign(db, *, start, days=10, revenue=10000, service=0):
    adv = await payer_org(db)
    cid = uuid.uuid4()
    items = [{"kind": "MEDIA", "campaign_id": cid, "description": "mídia", "amount_cents": revenue,
              "service_start": start, "service_end": start + timedelta(days=days - 1)}]
    if service:
        items.append({"kind": "SERVICE", "campaign_id": cid, "description": "arte", "amount_cents": service,
                      "refundable": True})
    order = await create_order(db, adv, items, revenue + service)
    assert await pay(db, order, fee_cents=0) == "PROCESSED"
    org = await make_org(db, business_roles=["PARTNER"])
    est = await make_establishment(db, org)
    scr = await make_screen(db, est)
    plan_provider({cid: {scr.id: {start + timedelta(days=i): 100 for i in range(days)}}})
    return adv, order, cid, est, scr


async def _approve_refund(db, a, b, ref_id, mail, *, payload_extra=None):
    from tests.fin_helpers import approve_svc

    req = await approvals.create_request(db, await ctx_for(db, a), "REFUND_PIX_OUT",
                                         {"refund_id": str(ref_id), **(payload_extra or {"await_provider_refund": True})},
                                         "Reembolso conforme política de cancelamento")
    await db.commit()
    return await approve_svc(db, b, req, mail)


async def test_before_start_full_refund_blocked_external_dependency(db, fin_env, mail):
    a, b = await make_admin_masters(db)
    adv, order, cid, est, scr = await campaign(db, start=TODAY + timedelta(days=5), service=1500)
    staff = await make_user(db, role="financial")
    ref = await refunds.request_refund(db, await ctx_for(db, staff), order.id, None, "cliente desistiu antes do início")
    await db.commit()
    assert (ref.policy, ref.refund_cents, ref.media_refund_cents, ref.services_refund_cents) == (
        "FULL_BEFORE_START", 11500, 10000, 1500)
    assert ref.status == "AWAITING_APPROVAL"
    assert await balance(db, "ADV:RefundPayable", adv.id) == 0  # nada sai sem dupla aprovação
    req = await _approve_refund(db, a, b, ref.id, mail)
    assert req.execution_state == "BLOCKED_EXTERNAL"
    db.expunge_all()
    ref = await db.get(Refund, ref.id)
    assert ref.status == "BLOCKED_EXTERNAL_DEPENDENCY"
    assert await balance(db, "ADV:DeferredRevenue", adv.id) == 0
    assert await balance(db, "ADV:RefundPayable", adv.id) == 11500
    assert await balance(db, "RD:ContraRevenue:Refunds") == -1500  # estorno de serviço reconhecido (natureza devedora)
    o = await db.get(PaymentOrder, order.id)
    assert (o.status, o.refunded_cents) == ("REFUNDED", 11500)
    (terms,) = await commission.terms_of(db, cid)
    assert (terms.status, terms.payment_order_id) == ("CANCELLED", order.id)


async def test_day_three_proportional_to_proof_of_play(db, fin_env, mail):
    a, b = await make_admin_masters(db)
    start = TODAY - timedelta(days=2)  # hoje é o 3º dia
    adv, order, cid, est, scr = await campaign(db, start=start)
    for i in range(3):
        await commission.accrue_commission_for_events(db, pop(cid, scr.id, start + timedelta(days=i), 100))
    await db.commit()
    q = await refunds.quote_refund(db, order.id, cid, TODAY)
    assert q["policy"] == "PROPORTIONAL" and q["parts"][0]["executed_ratio"] == "3/10"
    assert q["refund_cents"] == 7000 and q["parts"][0]["method"] == "PROOF_OF_PLAY"
    ref = await refunds.request_refund(db, await ctx_for(db, await make_user(db, role="financial")), order.id, cid,
                                       "cancelamento no 3º dia")
    await db.commit()
    accrued_before = sum((await db.execute(select(CommissionAccrual.amount_ucents))).scalars())
    await _approve_refund(db, a, b, ref.id, mail)
    assert await balance(db, "RD:Revenue:Media") == 3000          # parte executada reconhecida
    assert await balance(db, "ADV:RefundPayable", adv.id) == 7000
    assert await balance(db, "ADV:DeferredRevenue", adv.id) == 0
    rows = (await db.execute(select(CommissionAccrual))).scalars().all()
    assert sum(r.amount_ucents for r in rows) == accrued_before and all(r.reversed_ucents == 0 for r in rows)
    # após o cancelamento, PoP posterior não gera comissão
    r = await commission.accrue_commission_for_events(db, pop(cid, scr.id, TODAY + timedelta(days=1), 100))
    assert r["counted"] == 0


async def test_day_eight_denied_and_advertiser_cannot_override(db, fin_env, mail):
    adv, order, cid, est, scr = await campaign(db, start=TODAY - timedelta(days=7))
    q = await refunds.quote_refund(db, order.id, cid, TODAY)
    assert q["policy"] == "WINDOW_EXPIRED" and q["allowed"] is False
    advertiser = await make_user(db, role="advertiser", scope=("ORGANIZATION", adv.id))
    with pytest.raises(DomainError) as ei:
        await refunds.request_refund(db, await ctx_for(db, advertiser), order.id, cid, "quero cancelar no dia 8")
    assert ei.value.code == "REFUND_WINDOW_EXPIRED"
    with pytest.raises(DomainError) as ei:
        await refunds.request_refund(db, await ctx_for(db, advertiser), order.id, cid, "quero tudo de volta",
                                     override_amount_cents=10000)
    assert ei.value.status == 403


async def test_override_refund_reverts_pending_and_withdrawn_commission_goes_negative(db, fin_env, scope, mail):
    a, b = await make_admin_masters(db)
    prev_end = ledger.month_start(TODAY) - timedelta(days=1)
    start = ledger.month_start(prev_end)
    adv, order, cid, est, scr = await campaign(db, start=start, days=(prev_end - start).days + 1)
    # todo o mês anterior executado ⇒ comissão fechada, liberada e SACADA
    days = (prev_end - start).days + 1
    for i in range(days):
        await commission.accrue_commission_for_events(db, pop(cid, scr.id, start + timedelta(days=i), 100))
    await db.commit()
    await commission.close_competence(db, start)
    await commission.release_competence(db, start)
    await db.commit()
    paid_commission = await balance(db, "EST:CommissionPayable:Available", est.id)
    assert paid_commission == 500  # 5 % de R$ 100
    from app.core.crypto import encrypt_str
    from app.core.timeutils import utcnow
    from app.models.establishments import Establishment
    from app.models.financial import EstablishmentPixKey

    db.add(EstablishmentPixKey(id=uuid.uuid4(), establishment_id=est.id, key_type="cpf",
                               key_value_enc=encrypt_str("12345678909", purpose="pix_key", aad=f"est:{est.id}"),
                               key_fingerprint="2" * 64, key_masked="***", holder_name="T", holder_document_enc="v1:x",
                               holder_document_masked="***", active_from=utcnow(), created_by=a.id))
    await db.commit()
    w = await withdrawals.create_establishment_withdrawal(db, await db.get(Establishment, est.id), 500,
                                                          requested_by=a.id, idempotency_key="sacou")
    await db.commit()
    cpp.set_provider_override(cpp.CloudPayPagClient(provider_config(mode="live")))
    with respx.mock:
        respx.post("https://cloudpaypag.top/api/v1/integration/withdrawals").mock(return_value=httpx.Response(200, json={
            "withdrawal_id": 9, "external_id": "wd_paid_9", "status": "completed", "valor": 5.00, "taxa": 0.50,
            "valor_liquido": 4.50}))
        assert await withdrawals.execute_withdrawal(w.id, scope=scope) == "COMPLETED"
    # PoP pendente na competência aberta (este mês) para a mesma campanha (extensão hipotética de dias)
    (terms,) = await commission.terms_of(db, cid)
    open_row = CommissionAccrual(id=uuid.uuid4(), terms_id=terms.id, campaign_id=cid, screen_id=scr.id, establishment_id=est.id,
                                 local_day=TODAY, competence=ledger.month_start(TODAY), valid_count=1, expected_count=1,
                                 amount_ucents=7 * commission.UC, reversed_ucents=0)
    db.add(open_row)
    await db.commit()
    # decisão do admin fora da janela: reembolso integral com dupla aprovação
    fin = await make_user(db, role="financial")
    ref = await refunds.request_refund(db, await ctx_for(db, fin), order.id, cid, "decisão da diretoria",
                                       override_amount_cents=10000)
    await db.commit()
    assert ref.policy == "ADMIN_OVERRIDE"
    await _approve_refund(db, a, b, ref.id, mail)
    db.expunge_all()
    row = await db.get(CommissionAccrual, open_row.id)
    assert row.reversed_ucents == row.amount_ucents  # comissão pendente revertida
    assert await balance(db, "EST:Receivable", est.id) == 500  # já sacada ⇒ saldo negativo
    from app.models.establishments import Establishment as E2

    with pytest.raises(DomainError) as ei:
        await withdrawals.create_establishment_withdrawal(db, await db.get(E2, est.id), 1, requested_by=a.id,
                                                          idempotency_key="bloq")
    assert ei.value.code == "WITHDRAWAL_BLOCKED_NEGATIVE_BALANCE"
    bal = await commission.establishment_balance(db, est.id)
    assert bal["withdrawals_blocked"] is True and bal["negative_balance_cents"] == 500


async def test_refund_paid_by_pix_out_with_rd_bearing_fee(db, fin_env, scope, mail):
    a, b = await make_admin_masters(db)
    adv, order, cid, est, scr = await campaign(db, start=TODAY + timedelta(days=3))
    ref = await refunds.request_refund(db, await ctx_for(db, await make_user(db, role="financial")), order.id, cid,
                                       "cliente cancelou antes do início")
    await db.commit()
    await _approve_refund(db, a, b, ref.id, mail, payload_extra={"pix_key": "cliente@ex.com",
                                                                 "pix_key_type": "email"})
    w = (await db.execute(select(Withdrawal))).scalar_one()
    assert (w.kind, w.amount_cents, w.status) == ("REFUND", 10000, "REQUESTED")
    cpp.set_provider_override(cpp.CloudPayPagClient(provider_config(mode="live")))
    with respx.mock:
        respx.post("https://cloudpaypag.top/api/v1/integration/withdrawals/simulate").mock(return_value=httpx.Response(
            200, json={"valor": 100.00, "taxa": 2.00, "valor_liquido": 98.00, "saldo_disponivel": 500.00,
                       "suficiente_saldo_subconta": True, "saque_operacionalmente_apto": True, "suficiente": True,
                       "chave_propria": False}))
        post = respx.post("https://cloudpaypag.top/api/v1/integration/withdrawals").mock(return_value=httpx.Response(
            200, json={"withdrawal_id": 3, "external_id": "wd_ref_3", "status": "completed", "valor": 102.00,
                       "taxa": 2.00, "valor_liquido": 100.00}))
        assert await withdrawals.execute_withdrawal(w.id, scope=scope) == "COMPLETED"
        assert b'"valor":102.00' in post.calls.last.request.content  # pagador recebe o valor integral
    db.expunge_all()
    assert (await db.get(Refund, ref.id)).status == "PAID"
    assert await balance(db, "ADV:RefundPayable", adv.id) == 0
    assert await balance(db, "RD:Cash:CloudPayPag") == 10000 - 10200
