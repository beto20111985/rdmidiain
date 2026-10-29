"""Financeiro completo: reconhecimento diário de receita (último dia fecha o restante),
DRE, fluxo de caixa, extrato, visão geral, fechamento mensal com snapshot/hash e período
imutável; conciliação (divergência ⇒ item; resolução só por ajuste aprovado)."""
import uuid
from datetime import timedelta

import httpx
import respx
from sqlalchemy import func, select

from app.models.financial import (
    FinancialPeriodClosing,
    LedgerTransaction,
    ReconciliationItem,
    RevenueSchedule,
)
from app.modules.financial import cloudpaypag as cpp, ledger, revenue
from tests.factories import login_as, make_user, totp_code
from tests.fin_helpers import (  # noqa: F401
    balance,
    create_order,
    ctx_for,
    fin_env,
    make_admin_masters,
    media_item,
    pay,
    payer_org,
    provider_config,
)

TODAY = ledger.today_local()


async def test_daily_revenue_recognition_last_day_closes_remainder(db, fin_env):
    adv = await payer_org(db)
    start = TODAY - timedelta(days=20)
    order = await create_order(db, adv, [media_item(uuid.uuid4(), 1001, start, start + timedelta(days=2))], 1001)
    await pay(db, order, fee_cents=0)
    sch = (await db.execute(select(RevenueSchedule))).scalar_one()
    r1 = await revenue.recognize_revenue(db, through=start)
    await db.commit()
    assert r1["recognized_cents"] == 333  # floor(1001·1/3)
    again = await revenue.recognize_revenue(db, through=start)
    await db.commit()
    assert again["recognized_cents"] == 0  # idempotente
    await revenue.recognize_revenue(db, through=start + timedelta(days=1))
    await revenue.recognize_revenue(db, through=start + timedelta(days=5))
    await db.commit()
    assert await revenue.recognized_cents(db, sch.id) == 1001  # último dia leva o restante, soma exata
    assert await balance(db, "ADV:DeferredRevenue", adv.id) == 0
    assert await balance(db, "RD:Revenue:Media") == 1001
    comps = sorted((await db.execute(select(LedgerTransaction.competence_date).where(
        LedgerTransaction.kind == "REVENUE_RECOGNITION"))).scalars())
    assert comps == [start, start + timedelta(days=1), start + timedelta(days=2)]
    db.expunge_all()
    assert (await db.get(RevenueSchedule, sch.id)).status == "COMPLETED"


async def test_dre_cash_flow_statement_overview_and_period_closing(db, client, fin_env):
    adv = await payer_org(db)
    prev_end = ledger.month_start(TODAY) - timedelta(days=1)
    start = ledger.month_start(prev_end)
    order = await create_order(db, adv, [media_item(uuid.uuid4(), 10000, start, start)], 10000)
    await pay(db, order, fee_cents=300)
    await revenue.recognize_revenue(db, through=prev_end)
    await db.commit()
    fin = await make_user(db, role="financial", mfa=True)
    s = await login_as(client, fin)
    dre = (await client.get("/api/v1/financial/dre", params={"start": str(start), "end": str(TODAY)}, headers=s.headers)).json()
    lines = {line["group"]: line["amount_cents"] for line in dre["lines"]}
    assert lines["RECEITA_MIDIA"] == 10000 and lines["TAXAS_GATEWAY"] == -300
    cf = (await client.get("/api/v1/financial/cash-flow", params={"start": str(start), "end": str(TODAY)},
                           headers=s.headers)).json()
    assert cf["closing_balance_cents"] == 9700 and sum(r["inflow_cents"] for r in cf["rows"]) == 9700
    ov = (await client.get("/api/v1/financial/overview", params={"start": str(start), "end": str(TODAY)},
                           headers=s.headers)).json()
    assert ov["received_cents"] == 10000 and ov["gateway_fees_cents"] == 300 and ov["cash_cloudpaypag_cents"] == 9700
    accs = (await client.get("/api/v1/financial/accounts", headers=s.headers)).json()
    assert accs["check_sum_cents"] == 0  # balancete fecha
    st = (await client.get(f"/api/v1/financial/organizations/{adv.id}/statement", headers=s.headers)).json()
    assert st["total"] >= 2 and any(i["account_code"] == "ADV:DeferredRevenue" for i in st["items"])
    # fechamento do mês anterior: snapshot + hash; lançamentos posteriores para aquele mês são recusados/deslocados
    admin_fin = await make_user(db, role="financial", mfa=True)
    s2 = await login_as(client, admin_fin)
    r = await client.post("/api/v1/financial/closings", json={"period": str(start)}, headers=s2.headers)
    assert r.status_code == 201, r.text
    c = (await db.execute(select(FinancialPeriodClosing))).scalar_one()
    assert len(c.snapshot_sha256) == 64 and c.transaction_count >= 1 and c.snapshot["trial_balance"]
    r = await client.post("/api/v1/financial/closings", json={"period": str(start)}, headers=s2.headers)
    assert r.status_code == 409
    r = await client.post("/api/v1/financial/closings", json={"period": str(TODAY)}, headers=s2.headers)
    assert r.status_code == 409  # mês corrente ainda não terminou
    adv2 = await payer_org(db)
    o2 = await create_order(db, adv2, [media_item(uuid.uuid4(), 500, start, start)], 500)
    await pay(db, o2, fee_cents=0)
    await revenue.recognize_revenue(db, through=prev_end)
    await db.commit()
    late = (await db.execute(select(LedgerTransaction.competence_date).where(
        LedgerTransaction.org_id == adv2.id))).scalars().all()
    assert late and all(d > prev_end for d in late)  # período fechado não recebe lançamentos


async def test_reconciliation_divergence_requires_approved_adjustment(db, client_factory, fin_env, mail):
    a, b = await make_admin_masters(db)
    adv = await payer_org(db)
    order = await create_order(db, adv, [media_item(uuid.uuid4(), 10000, TODAY, TODAY)], 10000)
    await pay(db, order, fee_cents=299)  # caixa interno: 9701
    cpp.set_provider_override(cpp.CloudPayPagClient(provider_config(mode="live")))
    base = "https://cloudpaypag.top/api/v1/integration"
    fin = await make_user(db, role="financial", mfa=True)
    cf = client_factory()
    sf = await login_as(cf, fin)
    with respx.mock:
        respx.get(f"{base}/saldo").mock(return_value=httpx.Response(200, json={
            "subconta_id": 2, "external_id": "x", "saldo_disponivel": 97.01, "saldo_bloqueado": 0.00, "saldo_total": 97.01}))
        respx.get(f"{base}/balance/blocks").mock(return_value=httpx.Response(200, json={
            "subconta_id": 2, "external_id": "x", "saldo_disponivel": 97.01, "saldo_bloqueado": 0.00,
            "saldo_total": 97.01, "api_blocked_total": 0.00}))
        r = await cf.post("/api/v1/financial/reconciliation/runs", headers=sf.headers)
        assert r.status_code == 201 and r.json()["status"] == "COMPLETED"
        assert (await db.execute(select(func.count()).select_from(ReconciliationItem))).scalar_one() == 0
        respx.get(f"{base}/saldo").mock(return_value=httpx.Response(200, json={
            "subconta_id": 2, "external_id": "x", "saldo_disponivel": 100.00, "saldo_bloqueado": 0.00, "saldo_total": 100.00}))
        r = await cf.post("/api/v1/financial/reconciliation/runs", headers=sf.headers)
        assert r.status_code == 201
    item = (await db.execute(select(ReconciliationItem))).scalar_one()
    assert (item.kind, item.status, item.internal_amount_cents, item.external_amount_cents) == (
        "BALANCE_MISMATCH", "DIVERGENT", 9701, 10000)
    # financeiro não resolve sozinho (não há rota de resolução direta; pedido exige Admin Master)
    r = await cf.post(f"/api/v1/financial/reconciliation/items/{item.id}/resolution",
                      json={"legs": [], "reason": "tentativa de resolver sem aprovação"}, headers=sf.headers)
    assert r.status_code == 403
    ca, cb = client_factory(), client_factory()
    sa, sb = await login_as(ca, a), await login_as(cb, b)
    legs = [{"account_code": "RD:Cash:CloudPayPag", "amount_cents": 299},
            {"account_code": "RD:Income:Adjustments", "amount_cents": -299}]
    r = await ca.post(f"/api/v1/financial/reconciliation/items/{item.id}/resolution",
                      json={"legs": legs, "reason": "taxa estornada pela CloudPayPag (conferido no painel)"},
                      headers=sa.headers)
    assert r.status_code == 202, r.text
    req = r.json()
    db.expunge_all()
    assert (await db.get(ReconciliationItem, item.id)).status == "DIVERGENT"  # nada muda antes da aprovação
    from tests.fin_helpers import http_approval_code

    ecode = await http_approval_code(cb, sb, req["id"], mail, b)
    r = await cb.post(f"/api/v1/financial/approvals/{req['id']}/approve", json={
        "totp_code": totp_code(b), "financial_password": b.fin_password, "payload_sha256": req["payload_sha256"],
        "email_code": ecode},
        headers=sb.headers)
    assert r.status_code == 200, r.text
    db.expunge_all()
    it = await db.get(ReconciliationItem, item.id)
    assert it.status == "RESOLVED" and it.resolution_approval_id == uuid.UUID(req["id"]) and it.resolution_transaction_id
    assert await balance(db, "RD:Cash:CloudPayPag") == 10000
