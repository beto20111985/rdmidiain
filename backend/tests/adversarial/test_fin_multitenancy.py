"""IDOR/multitenancy financeiro: anunciante A não lê faturas/comprovantes/extratos/reembolsos
de B; estabelecimento não lê comissões/saldo de outro; clientes não acessam ledger/contas/
conciliação; nenhuma listagem vaza outra organização."""
import uuid

from app.modules.financial import commission, ledger
from tests.factories import login_as, make_establishment, make_org, make_user
from tests.fin_helpers import create_order, fin_env, make_screen, media_item, pay, payer_org, plan_provider, pop  # noqa: F401

F = "/api/v1/financial"
TODAY = ledger.today_local()


async def _paid(db, org, amount=5000):
    o = await create_order(db, org, [media_item(uuid.uuid4(), amount, TODAY, TODAY)], amount)
    await pay(db, o)
    return o


async def test_advertiser_cannot_read_other_org_financial_data(db, client_factory, fin_env):
    org_a, org_b = await payer_org(db), await payer_org(db)
    oa, ob = await _paid(db, org_a), await _paid(db, org_b, 7000)
    ua = await make_user(db, role="advertiser", scope=("ORGANIZATION", org_a.id))
    c = client_factory()
    s = await login_as(c, ua)
    lst = (await c.get(f"{F}/invoices", headers=s.headers)).json()
    assert {i["id"] for i in lst["items"]} == {str(oa.id)} and lst["total"] == 1
    assert (await c.get(f"{F}/invoices", params={"org_id": str(org_b.id)}, headers=s.headers)).json()["total"] == 0
    assert (await c.get(f"{F}/invoices/{oa.id}", headers=s.headers)).status_code == 200
    for url in (f"{F}/invoices/{ob.id}", f"{F}/invoices/{ob.id}/receipt", f"{F}/organizations/{org_b.id}/statement"):
        assert (await c.get(url, headers=s.headers)).status_code == 404, url
    r = await c.get(f"{F}/invoices/{oa.id}/receipt", headers=s.headers)
    assert r.status_code == 200 and r.content[:4] == b"%PDF" and len(r.headers["X-Content-SHA256"]) == 64
    assert (await c.post(f"{F}/invoices/{ob.id}/fiscal-document", headers=s.headers)).status_code == 404
    r = await c.post(f"{F}/invoices/{oa.id}/fiscal-document", headers=s.headers)
    assert r.status_code == 202 and r.json()["status"] == "BLOCKED_EXTERNAL_DEPENDENCY"
    assert (await c.post(f"{F}/refunds/quote", json={"payment_order_id": str(ob.id)}, headers=s.headers)).status_code == 404
    st = (await c.get(f"{F}/organizations/{org_a.id}/statement", headers=s.headers)).json()
    assert st["total"] > 0
    for url in (f"{F}/ledger/transactions", f"{F}/accounts", f"{F}/overview", f"{F}/receivables",
                f"{F}/reconciliation/items", f"{F}/approvals", f"{F}/dre", f"{F}/provider/status"):
        assert (await c.get(url, headers=s.headers)).status_code == 403, url


async def test_establishment_sees_only_own_commissions(db, client_factory, fin_env):
    adv = await payer_org(db)
    cid = uuid.uuid4()
    o = await create_order(db, adv, [media_item(cid, 5000, TODAY, TODAY)], 5000)
    await pay(db, o)
    org_a, org_b = await make_org(db, business_roles=["PARTNER"]), await make_org(db, business_roles=["PARTNER"])
    ea, eb = await make_establishment(db, org_a), await make_establishment(db, org_b)
    sa, sb = await make_screen(db, ea), await make_screen(db, eb)
    plan_provider({cid: {sa.id: {TODAY: 10}, sb.id: {TODAY: 10}}})
    await commission.accrue_commission_for_events(db, pop(cid, sa.id, TODAY, 10) + pop(cid, sb.id, TODAY, 10))
    await db.commit()
    owner_a = await make_user(db, role="establishment_owner", scope=("ORGANIZATION", org_a.id))
    c = client_factory()
    s = await login_as(c, owner_a)
    items = (await c.get(f"{F}/commissions", params={"group_by": "screen"}, headers=s.headers)).json()["items"]
    assert [i["screen"] if "screen" in i else i["screen_id"] for i in items] == [str(sa.id)]
    assert items[0]["valid_plays"] == 10 and items[0]["amount_cents"] == 125 and items[0]["status"] == "PENDENTE"
    assert "pool_cents" not in items[0] and "eligible_revenue_cents" not in items[0]  # sem dados comerciais
    r = await c.get(f"{F}/commissions", params={"establishment_id": str(eb.id)}, headers=s.headers)
    assert r.status_code == 404
    bal = (await c.get(f"{F}/establishments/{ea.id}/balance", headers=s.headers)).json()
    assert bal["pending_provisional_cents"] == 125 and bal["available_cents"] == 0
    assert (await c.get(f"{F}/establishments/{eb.id}/balance", headers=s.headers)).status_code == 404
    models = await c.get(f"{F}/commissions/models", headers=s.headers)
    assert models.status_code == 200 and models.json()["pool_bps"] == 500
