"""API financeira diversa: senha financeira (só o próprio usuário; login+TOTP), status da
integração (dependências externas), simulação de webhook SOMENTE em development (assinada
e pelo caminho real), exceção de comissão e ajuste via dupla aprovação."""
import uuid

from sqlalchemy import select

from app.core.config import settings
from app.core.passwords import verify_password
from app.models.financial import EstablishmentCommissionSetting, PaymentOrder, WebhookEvent
from app.models.users import User
from app.modules.financial import commission, payments
from tests.factories import login_as, make_establishment, make_org, make_user, totp_code
from tests.fin_helpers import balance, create_order, fin_env, make_admin_masters, media_item, payer_org  # noqa: F401

F = "/api/v1/financial"


async def test_financial_password_set_and_change_by_self_only(db, client, fin_env):
    u = await make_user(db, role="establishment_owner", scope=("ORGANIZATION", (await make_org(db)).id), mfa=True)
    s = await login_as(client, u)
    st = (await client.get(f"{F}/security/status", headers=s.headers)).json()
    assert st == {"totp_enabled": True, "financial_password_set": False, "financial_password_changed_at": None,
                  "reset_blocked_until": None}
    r = await client.post(f"{F}/security/financial-password", json={
        "login_password": u.password, "totp_code": totp_code(u), "new_password": u.password}, headers=s.headers)
    assert r.status_code == 400 and r.json()["error"]["code"] == "FINANCIAL_PASSWORD_POLICY"  # ≠ senha de login
    r = await client.post(f"{F}/security/financial-password", json={
        "login_password": "senha-errada-1", "totp_code": totp_code(u), "new_password": "Fin-Segura-2026"}, headers=s.headers)
    assert r.status_code == 403
    r = await client.post(f"{F}/security/financial-password", json={
        "login_password": u.password, "totp_code": totp_code(u), "new_password": "Fin-Segura-2026"}, headers=s.headers)
    assert r.status_code == 204, r.text
    db.expunge_all()
    row = await db.get(User, u.id)
    assert verify_password("Fin-Segura-2026", row.financial_password_hash)[0]
    assert row.financial_password_hash != row.hashed_password
    # troca exige a senha financeira atual
    r = await client.post(f"{F}/security/financial-password", json={
        "login_password": u.password, "totp_code": totp_code(u), "new_password": "Outra-Fin-2027"}, headers=s.headers)
    assert r.status_code == 403


async def test_provider_status_shows_external_dependencies(db, client, fin_env):
    fin = await make_user(db, role="financial", mfa=True)
    s = await login_as(client, fin)
    st = (await client.get(f"{F}/provider/status", headers=s.headers)).json()
    assert st["mode"] == "FAKE" and st["fake_warning"]
    assert st["capabilities"]["charge_refund"] == "BLOQUEADO POR DEPENDÊNCIA EXTERNA"
    assert st["capabilities"]["charge_boleto"] == "BLOQUEADO POR DEPENDÊNCIA EXTERNA"
    assert st["capabilities"]["charge_pix"] == "DISPONÍVEL"
    from tests.fin_helpers import TEST_SECRET, TEST_TOKEN

    raw = (await client.get(f"{F}/provider/status", headers=s.headers)).text
    assert TEST_TOKEN not in raw and TEST_SECRET not in raw and st["token_configured"] is True


async def test_dev_simulate_webhook_goes_through_real_ingest_in_development(db, client, fin_env, monkeypatch):
    adv = await payer_org(db)
    order = await create_order(db, adv, [media_item(uuid.uuid4(), 3000, __import__("datetime").date.today(),
                                                    __import__("datetime").date.today())], 3000)
    admin = await make_user(db, role="super_admin", mfa=True)
    s = await login_as(client, admin)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    r = await client.post(f"{F}/dev/simulate-webhook", json={"payment_order_id": str(order.id), "fee_cents": 90},
                          headers=s.headers)
    assert r.status_code == 200, r.text
    assert r.json()["simulated"] is True and r.json()["provider_mode"] == "FAKE"
    ev = (await db.execute(select(WebhookEvent))).scalar_one()
    assert ev.signature_valid and ev.status == "RECEIVED"  # não muda status direto: worker processa
    db.expunge_all()
    assert (await db.get(PaymentOrder, order.id)).status == "PENDING"
    assert await payments.process_webhook_event(db, ev.id) == "PROCESSED"
    await db.commit()
    db.expunge_all()
    assert (await db.get(PaymentOrder, order.id)).status == "PAID"


async def test_commission_exception_and_adjustment_via_dual_approval(db, client_factory, fin_env, mail):
    a, b = await make_admin_masters(db)
    org = await make_org(db, business_roles=["PARTNER"])
    est = await make_establishment(db, org)
    ca, cb = client_factory(), client_factory()
    sa, sb = await login_as(ca, a), await login_as(cb, b)
    r = await ca.post(f"{F}/approvals", json={"action_type": "COMMISSION_EXCEPTION", "payload": {
        "establishment_id": str(est.id), "rate_bps": 700}, "reason": "parceiro premium com exceção de 7%"},
        headers=sa.headers)
    assert r.status_code == 201, r.text
    req = r.json()
    eff = await commission.effective_terms(db, est.id, 500)
    assert eff.rate_bps == 500  # nada muda antes da aprovação
    from tests.fin_helpers import http_approval_code

    r = await cb.post(f"{F}/approvals/{req['id']}/approve", json={
        "totp_code": totp_code(b), "financial_password": b.fin_password, "payload_sha256": req["payload_sha256"],
        "email_code": await http_approval_code(cb, sb, req["id"], mail, b)}, headers=sb.headers)
    assert r.status_code == 200, r.text
    exc = (await db.execute(select(EstablishmentCommissionSetting))).scalar_one()
    assert exc.rate_bps == 700 and exc.approved_by == b.id and exc.approval_request_id == uuid.UUID(req["id"])
    assert (await commission.effective_terms(db, est.id, 500)).rate_bps == 700
    # ajuste manual de comissão (crédito) também só com o outro sócio
    r = await cb.post(f"{F}/approvals", json={"action_type": "COMMISSION_ADJUSTMENT", "payload": {
        "establishment_id": str(est.id), "amount_cents": 150, "description": "acerto de exibições"},
        "reason": "acerto de exibições comprovadas"}, headers=sb.headers)
    req2 = r.json()
    r = await ca.post(f"{F}/approvals/{req2['id']}/approve", json={
        "totp_code": totp_code(a), "financial_password": a.fin_password, "payload_sha256": req2["payload_sha256"],
        "email_code": await http_approval_code(ca, sa, req2["id"], mail, a)}, headers=sa.headers)
    assert r.status_code == 200, r.text
    assert await balance(db, "EST:CommissionPayable:Available", est.id) == 150
    view = await ca.get(f"{F}/commission-settings/establishments/{est.id}", headers=sa.headers)
    assert view.json()["effective"]["rate_bps"] == 700 and len(view.json()["history"]) == 1


async def test_seller_commission_from_sales_attribution_on_payment(db, fin_env):
    """Comissão de vendedor (W-BIZ ``sales_attributions``) vira passivo SELLER no pagamento, uma única vez."""
    from app.models.crm import Proposal, SalesAttribution
    from tests.fin_helpers import pay

    seller = await make_user(db, role="sales")
    adv = await payer_org(db)
    order_ref = uuid.uuid4()
    prop = Proposal(id=uuid.uuid4(), number=f"P-{uuid.uuid4().hex[:6]}", org_id=adv.id, seller_id=seller.id,
                    title="Proposta")
    db.add(prop)
    await db.flush()
    db.add(SalesAttribution(id=uuid.uuid4(), proposal_id=prop.id, order_id=order_ref, seller_id=seller.id,
                            org_id=adv.id, amount_cents=8000, commission_bps=500, commission_cents=400))
    await db.commit()
    import datetime as dt

    o = await create_order(db, adv, [media_item(uuid.uuid4(), 8000, dt.date.today(), dt.date.today())], 8000,
                           ref_type="campaign_checkout", ref_id=str(order_ref))
    # pedido inexistente no W-CAMP ⇒ recusa de negócio: divergência registrada, não silenciosa; o financeiro segue
    assert await pay(db, o, event_id=5150) == "PROCESSED"
    await pay(db, o, event_id=5151)
    assert await balance(db, "SELLER:CommissionPayable", seller.id) == 400
    assert await balance(db, "RD:Expense:SellerCommission") == 400
    from app.models.financial import PaymentOrderCallback, ReconciliationItem

    cb = (await db.execute(select(PaymentOrderCallback))).scalar_one()
    assert cb.status == "REJECTED" and cb.last_error
    assert (await db.execute(select(ReconciliationItem.kind))).scalars().all() == ["PAID_WITHOUT_SERVICE"]


async def test_payable_settlement_external_via_dual_approval(db, fin_env, mail):
    import datetime as dt

    from app.models.financial import Payable
    from app.modules.financial import approvals
    from tests.fin_helpers import ctx_for

    a, b = await make_admin_masters(db)
    req = await approvals.create_request(db, await ctx_for(db, a), "EXPENSE_CREATE", {
        "description": "Aluguel", "category": "aluguel", "amount_cents": 250000,
        "competence_date": dt.date.today().isoformat(), "due_date": dt.date.today().isoformat()}, "Aluguel do escritório")
    await db.commit()
    from tests.fin_helpers import approve_svc

    await approve_svc(db, b, req, mail)
    pay_row = (await db.execute(select(Payable))).scalar_one()
    req2 = await approvals.create_request(db, await ctx_for(db, b), "PAYABLE_SETTLEMENT",
                                          {"payable_id": str(pay_row.id), "method": "EXTERNAL"}, "Pago via banco externo")
    await db.commit()
    await approve_svc(db, a, req2, mail)
    db.expunge_all()
    p = await db.get(Payable, pay_row.id)
    assert p.status == "PAID" and p.settlement_approval_id == req2.id
    assert await balance(db, "RD:AccountsPayable") == 0
    assert await balance(db, "RD:Cash:External") == -250000


async def test_withdrawal_simulation_endpoint_uses_provider_fee(db, client, fin_env):
    from tests.fin_helpers import est_with_owner

    org, est, owner = await est_with_owner(db)
    admin = await make_user(db, role="admin", mfa=True)
    sa = await login_as(client, admin)
    r = await client.post(f"{F}/establishments/{est.id}/pix-keys", json={
        "key_type": "email", "key_value": "dono@estab.com", "holder_name": "Dono", "holder_document": "12345678909",
        "reason": "cadastro inicial"}, headers=sa.headers)
    assert r.status_code == 201
    from sqlalchemy import text

    await db.execute(text("UPDATE establishment_pix_keys SET active_from = now() - interval '1 second'"))
    await db.commit()
    so = await login_as(client, owner)
    r = await client.post(f"{F}/withdrawals/simulate", json={"establishment_id": str(est.id), "amount_cents": 5000},
                          headers=so.headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fee_cents"] == 200 and body["net_cents"] == 4800 and body["sufficient"] is False  # saldo 0
