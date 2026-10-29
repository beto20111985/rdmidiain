"""Cobranças ao parceiro (mensalidade/caução/instalação/upgrade — 100 % CloudPayPag) e recusa
determinística de callback no ``paid`` (pedido superado ⇒ 409 ORDER_MISMATCH): divergência
para estorno/análise, nunca silenciosa nem reprocessada."""
import uuid

from sqlalchemy import func, select

from app.core.errors import Conflict
from app.models.financial import CommissionCampaign, PaymentOrder, PaymentOrderCallback, ReconciliationItem
from app.modules.financial import accounting, ledger, payments
from tests.factories import login_as, make_establishment, make_user
from tests.fin_helpers import balance, create_order, fin_env, media_item, pay, payer_org, scope  # noqa: F401

F = "/api/v1/financial"


async def _partner(db):
    org = await payer_org(db, business_roles=["PARTNER"])
    est = await make_establishment(db, org)
    owner = await make_user(db, role="establishment_owner", scope=("ORGANIZATION", org.id))
    return org, est, owner


async def test_partner_charges_deposit_is_liability_and_fee_is_revenue(db, client_factory, fin_env):
    org, est, owner = await _partner(db)
    fin = await make_user(db, role="financial", mfa=True)
    cf, co = client_factory(), client_factory()
    sf, so = await login_as(cf, fin), await login_as(co, owner)
    created = {}
    for kind, amount in (("DEPOSIT", 50000), ("MONTHLY_FEE", 9900), ("INSTALLATION_FEE", 15000)):
        r = await cf.post(f"{F}/partner-charges", json={"establishment_id": str(est.id), "kind": kind,
                                                        "amount_cents": amount, "description": f"Cobrança {kind}",
                                                        "source_type": "partner_plan", "source_id": str(uuid.uuid4())},
                          headers={**sf.headers, "Idempotency-Key": f"pc-{kind}"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["status"] == "PENDING" and body["pix_qr_code"] and body["eligible_revenue_cents"] == 0
        created[kind] = body["id"]
    # parceiro (dono do estabelecimento) não cria cobranças; vê as suas
    r = await co.post(f"{F}/partner-charges", json={"establishment_id": str(est.id), "kind": "DEPOSIT",
                                                    "amount_cents": 1, "description": "tentativa"}, headers=so.headers)
    assert r.status_code == 403
    lst = (await co.get(f"{F}/establishments/{est.id}/charges", headers=so.headers)).json()
    assert lst["total"] == 3
    for kind in created:
        o = await db.get(PaymentOrder, uuid.UUID(created[kind]))
        assert await pay(db, o, fee_cents=0) == "PROCESSED"
    assert await balance(db, "EST:DepositHeld", est.id) == 50000           # caução = passivo reembolsável
    assert await balance(db, "RD:Revenue:PartnerFees") == 9900             # mensalidade = receita
    assert await balance(db, "RD:Revenue:Services") == 15000               # instalação = receita de serviço
    assert await balance(db, "RD:Cash:CloudPayPag") == 74900
    assert (await db.execute(select(func.count()).select_from(CommissionCampaign))).scalar_one() == 0  # fora do pool
    today = ledger.today_local()
    dre = await accounting.dre(db, ledger.month_start(today), today)
    assert {line["group"]: line["amount_cents"] for line in dre["lines"]}["RECEITA_SERVICOS"] == 24900  # sem caução
    r = await co.get(f"{F}/establishments/{est.id}/charges/{created['DEPOSIT']}/receipt", headers=so.headers)
    assert r.status_code == 200 and r.content[:4] == b"%PDF"
    # outro parceiro não vê
    _, est2, owner2 = await _partner(db)
    c2 = client_factory()
    s2 = await login_as(c2, owner2)
    assert (await c2.get(f"{F}/establishments/{est.id}/charges", headers=s2.headers)).status_code == 404
    assert (await c2.get(f"{F}/establishments/{est.id}/charges/{created['DEPOSIT']}/receipt",
                         headers=s2.headers)).status_code == 404


async def test_superseded_order_paid_late_becomes_divergence_not_retried(db, fin_env):
    calls = []

    async def superseded(db_, order):
        calls.append(order.id)
        raise Conflict("ORDER_MISMATCH", "Pedido superado por outro pedido/revisão.")

    payments.register_paid_callback("test_superseded", "test.on_order_paid", superseded)
    adv = await payer_org(db)
    today = ledger.today_local()
    o = await create_order(db, adv, [media_item(uuid.uuid4(), 3000, today, today)], 3000, ref_type="test_superseded")
    assert await pay(db, o, fee_cents=0) == "PROCESSED"
    cb = (await db.execute(select(PaymentOrderCallback))).scalar_one()
    assert cb.status == "REJECTED" and "ORDER_MISMATCH" in cb.last_error
    item = (await db.execute(select(ReconciliationItem))).scalar_one()
    assert item.kind == "PAID_WITHOUT_SERVICE" and item.internal_amount_cents == 3000
    assert await balance(db, "RD:Cash:CloudPayPag") == 3000  # o dinheiro recebido está contabilizado
    assert await payments.retry_failed_callbacks(db) == 0      # nunca reprocessa recusa de negócio
    await db.commit()
    assert len(calls) == 1
    assert (await db.get(PaymentOrder, o.id)).status == "PAID"


async def _deposit_paid(db, amount=50000, *, key_ready=True):
    from datetime import timedelta

    from app.core.authz import AuthContext
    from app.core.crypto import encrypt_str
    from app.core.timeutils import utcnow
    from app.models.financial import EstablishmentPixKey

    org, est, owner = await _partner(db)
    o = await payments.create_partner_charge(db, AuthContext.system("system"), est.id, "DEPOSIT", amount,
                                             "Caução do comodato")
    await db.commit()
    assert await pay(db, o, fee_cents=0) == "PROCESSED"
    db.add(EstablishmentPixKey(id=uuid.uuid4(), establishment_id=est.id, key_type="cnpj",
                               key_value_enc=encrypt_str("12345678000199", purpose="pix_key", aad=f"est:{est.id}"),
                               key_fingerprint="9" * 64, key_masked="***456***99", holder_name="Parceiro",
                               holder_document_enc="v1:x", holder_document_masked="***",
                               active_from=utcnow() + (timedelta(seconds=-1) if key_ready else timedelta(hours=12)),
                               created_by=owner.id))
    await db.commit()
    return org, est


async def test_deposit_refund_partial_with_damage_dual_approval_pix_out(db, client_factory, fin_env, mail, scope):
    import re

    import httpx
    import respx

    from app.models.financial import FinancialApprovalRequest, Withdrawal
    from app.modules.financial import cloudpaypag as cpp
    from app.modules.financial import withdrawals
    from tests.factories import totp_code
    from tests.fin_helpers import make_admin_masters, provider_config

    org, est = await _deposit_paid(db)
    a, b = await make_admin_masters(db)
    ca, cb = client_factory(), client_factory()
    sa, sb = await login_as(ca, a), await login_as(cb, b)
    url = f"{F}/establishments/{est.id}/deposit-refunds"
    body = {"refund_cents": 30000, "retained_cents": 20000, "damage_description": "TV quebrada (tela trincada)",
            "reason": "Encerramento do comodato com dano na TV"}
    r1 = await ca.post(url, json=body, headers={**sa.headers, "Idempotency-Key": "dep-1"})
    r2 = await ca.post(url, json=body, headers={**sa.headers, "Idempotency-Key": "dep-1"})
    assert r1.status_code == 202, r1.text
    assert r2.status_code == 202 and r2.json()["id"] == r1.json()["id"]  # idempotente
    req = r1.json()
    r = await ca.post(url, json={**body, "refund_cents": 1, "retained_cents": 0}, headers=sa.headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "DEPOSIT_REFUND_EXCEEDS_HELD"  # já comprometida
    dep = (await ca.get(f"{F}/establishments/{est.id}/deposit", headers=sa.headers)).json()
    assert dep == {"establishment_id": str(est.id), "held_cents": 50000, "available_for_refund_cents": 0}
    approve_url = f"{F}/approvals/{req['id']}/approve"
    r = await ca.post(approve_url, json={"totp_code": totp_code(a), "financial_password": a.fin_password,
                                         "payload_sha256": req["payload_sha256"]}, headers=sa.headers)
    assert r.status_code == 403 and r.json()["error"]["code"] == "SELF_APPROVAL_FORBIDDEN"
    from tests.fin_helpers import http_approval_code

    code = await http_approval_code(cb, sb, req["id"], mail, b)
    r = await cb.post(approve_url, json={"totp_code": totp_code(b), "financial_password": b.fin_password,
                                         "payload_sha256": req["payload_sha256"], "email_code": code}, headers=sb.headers)
    assert r.status_code == 200, r.text
    assert await balance(db, "RD:Revenue:Services") == 20000  # dano retido vira receita de serviço
    assert await balance(db, "EST:DepositHeld", est.id) == 30000
    w = (await db.execute(select(Withdrawal))).scalar_one()
    assert (w.kind, w.amount_cents, w.establishment_id) == ("DEPOSIT", 30000, est.id)
    cpp.set_provider_override(cpp.CloudPayPagClient(provider_config(mode="live")))
    base = "https://cloudpaypag.top/api/v1/integration"
    with respx.mock:
        respx.post(f"{base}/withdrawals/simulate").mock(return_value=httpx.Response(200, json={
            "valor": 300.00, "taxa": 2.00, "valor_liquido": 298.00, "saldo_disponivel": 500.00,
            "suficiente_saldo_subconta": True, "saque_operacionalmente_apto": True, "suficiente": True,
            "chave_propria": False}))
        post = respx.post(f"{base}/withdrawals").mock(return_value=httpx.Response(200, json={
            "withdrawal_id": 77, "external_id": "wd_dep_77", "status": "completed", "valor": 302.00, "taxa": 2.00,
            "valor_liquido": 300.00}))
        assert await withdrawals.execute_withdrawal(w.id, scope=scope) == "COMPLETED"
        assert await withdrawals.execute_withdrawal(w.id, scope=scope) == "COMPLETED"
        assert post.call_count == 1  # sem retry/duplicidade
        assert b'"pix_key":"12345678000199","pix_key_type":"cnpj"' in post.calls.last.request.content
    assert await balance(db, "EST:DepositHeld", est.id) == 0
    assert await balance(db, "RD:Cash:CloudPayPag") == 50000 - 30200
    db.expunge_all()
    assert (await db.get(FinancialApprovalRequest, uuid.UUID(req["id"]))).execution_state == "EXECUTED"


async def test_deposit_refund_respects_held_amount_damage_text_and_pix_key_cooldown(db, fin_env, mail):
    import re

    import pytest

    from app.core.authz import AuthContext
    from app.core.errors import DomainError
    from app.modules.financial import approvals
    from tests.factories import totp_code
    from tests.fin_helpers import ctx_for, make_admin_masters

    org, est = await _deposit_paid(db, 10000, key_ready=False)
    est_id = est.id
    a, b = await make_admin_masters(db)
    with pytest.raises(DomainError) as ei:
        await approvals.create_request(db, await ctx_for(db, a), "DEPOSIT_REFUND",
                                       {"establishment_id": str(est_id), "refund_cents": 10001},
                                       "Devolução maior que a caução")
    assert ei.value.code == "DEPOSIT_REFUND_EXCEEDS_HELD"
    await db.rollback()
    with pytest.raises(DomainError) as ei:
        await approvals.create_request(db, await ctx_for(db, a), "DEPOSIT_REFUND",
                                       {"establishment_id": str(est_id), "refund_cents": 0, "retained_cents": 500},
                                       "Retenção sem descrever o dano")
    assert ei.value.code == "DEPOSIT_DAMAGE_DESCRIPTION_REQUIRED"
    await db.rollback()
    # retenção integral (sem PIX OUT ⇒ sem código por e-mail), aprovada pelo outro sócio
    req = await approvals.create_request(db, await ctx_for(db, a), "DEPOSIT_REFUND",
                                         {"establishment_id": str(est_id), "refund_cents": 0, "retained_cents": 10000,
                                          "damage_description": "TV quebrada paga pelo estabelecimento"},
                                         "Caução retida integralmente por dano")
    await db.commit()
    from tests.fin_helpers import approve_svc

    req = await approve_svc(db, b, req, mail)
    assert req.execution_state == "EXECUTED"
    assert await balance(db, "EST:DepositHeld", est_id) == 0 and await balance(db, "RD:Revenue:Services") == 10000
    # nova caução; chave PIX ainda no bloqueio de 12 h ⇒ a aprovação do PIX OUT é recusada (423)
    o2 = await payments.create_partner_charge(db, AuthContext.system("system"), est_id, "DEPOSIT", 4000, "Nova caução")
    await db.commit()
    assert await pay(db, o2, fee_cents=0) == "PROCESSED"
    req2 = await approvals.create_request(db, await ctx_for(db, a), "DEPOSIT_REFUND",
                                          {"establishment_id": str(est_id), "refund_cents": 4000},
                                          "Devolução da nova caução")
    await db.commit()
    await approvals.request_email_code(db, await ctx_for(db, b), req2.id)
    await db.commit()
    from tests.fin_helpers import last_code

    code = last_code(mail, b)
    with pytest.raises(DomainError) as ei:
        await approvals.approve(db, await ctx_for(db, b), req2.id, totp_code=totp_code(b),
                                financial_password=b.fin_password, payload_sha256=req2.payload_sha256, email_code=code)
    assert ei.value.code == "PIX_KEY_COOLDOWN" and ei.value.status == 423
    await db.rollback()
    assert await balance(db, "EST:DepositHeld", est_id) == 4000  # nada saiu
