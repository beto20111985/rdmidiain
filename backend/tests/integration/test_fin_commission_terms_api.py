"""``GET /api/v1/financial/commissions/campaigns/{id}`` — termos de comissão POR PAGAMENTO no painel.

Domínios REAIS (W-CAMP checkout + renovação, W-INV booking, W-FIN). Prova: visão completa para a
equipe financeira (escopo GLOBAL); estabelecimento vê só termos em que participa e só a SUA comissão,
sem cobrança/receita/pool do anunciante; estabelecimento fora da campanha e anunciante ⇒ 404/403;
número FIXO de consultas (sem N+1)."""
import uuid

import pytest
from sqlalchemy import event, select

from app.models.financial import CommissionAccrual
from app.modules.financial import commission
from tests.factories import login_as, make_establishment, make_org, make_user
from tests.fin_helpers import fin_env, pop  # noqa: F401
from tests.integration.test_fin_commission_terms import _accrued, _refund, _renewed_campaign

pytestmark = pytest.mark.usefixtures("fin_env")

URL = "/api/v1/financial/commissions/campaigns/{}"
COMMERCIAL = {"payment_order_id", "eligible_revenue_cents", "pool_cents", "released_cents", "model", "pool_bps",
              "expected_exhibitions", "registered_at"}


async def _accrue_first_days(db, cid, screens, old_end):
    plan = await commission.contracted_plan(db, cid)
    for sc in screens:
        d_old = min(plan[sc.id])
        d_new = min(d for d in plan[sc.id] if d > old_end)
        for d in (d_old, d_new):
            await commission.accrue_commission_for_events(db, pop(cid, sc.id, d, plan[sc.id][d]))
    await db.commit()


async def test_finance_sees_full_terms_establishment_only_its_commission(db, client_factory, mail):
    c0 = client_factory()
    cid, screens, ests, old_end, new_end, po1, po2 = await _renewed_campaign(db, c0)
    await _accrue_first_days(db, cid, screens, old_end)
    t1 = await commission.terms_for_payment(db, cid, po1.id)
    t2 = await commission.terms_for_payment(db, cid, po2.id)

    fin = await make_user(db, role="financial", mfa=True)
    cf = client_factory()
    r = await cf.get(URL.format(cid), headers=(await login_as(cf, fin)).headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["visibility"] == "FULL" and body["campaign_id"] == str(cid)
    by_kind = {t["kind"]: t for t in body["terms"]}
    assert [t["kind"] for t in body["terms"]] == ["INITIAL", "RENEWAL"]  # ordem do período
    for t, po, terms in ((by_kind["INITIAL"], po1, t1), (by_kind["RENEWAL"], po2, t2)):
        assert t["payment_order_id"] == str(po.id) and t["eligible_revenue_cents"] == po.eligible_revenue_cents
        assert t["pool_cents"] == po.eligible_revenue_cents * 500 // 10_000
        assert (t["period_start"], t["period_end"], t["status"]) == (
            terms.start_date.isoformat(), terms.end_date.isoformat(), "ACTIVE")
        assert t["released_cents"] == 0 and t["clawback_cents"] == 0
        assert t["commission_ucents"] == await _accrued(db, terms.id) > 0
    assert body["totals"]["pool_cents"] == t1.pool_cents + t2.pool_cents

    # dono do estabelecimento 0: só a SUA comissão, sem dados comerciais do anunciante
    owner = await make_user(db, role="establishment_owner", scope=("ORGANIZATION", ests[0].org_id), mfa=True)
    co = client_factory()
    r = await co.get(URL.format(cid), headers=(await login_as(co, owner)).headers)
    assert r.status_code == 200, r.text
    eb = r.json()
    assert eb["visibility"] == "ESTABLISHMENT" and [t["kind"] for t in eb["terms"]] == ["INITIAL", "RENEWAL"]
    for t, terms in zip(eb["terms"], (t1, t2)):
        assert not COMMERCIAL & set(t), set(t)
        own = sum((await db.execute(select(CommissionAccrual.amount_ucents - CommissionAccrual.reversed_ucents).where(
            CommissionAccrual.terms_id == terms.id, CommissionAccrual.establishment_id == ests[0].id))).scalars())
        assert t["commission_ucents"] == own > 0
        assert t["commission_ucents"] < await _accrued(db, terms.id)  # a outra tela (outro estabelecimento) não entra
    assert not COMMERCIAL & set(eb["totals"])
    assert str(po1.id) not in r.text and str(po2.id) not in r.text

    # estabelecimento fora da campanha ⇒ 404; anunciante (sem commissions.view) ⇒ 403
    other_org = await make_org(db, business_roles=["PARTNER"])
    await make_establishment(db, other_org)
    stranger = await make_user(db, role="establishment_owner", scope=("ORGANIZATION", other_org.id), mfa=True)
    cs = client_factory()
    assert (await cs.get(URL.format(cid), headers=(await login_as(cs, stranger)).headers)).status_code == 404
    cf2 = client_factory()
    assert (await cf2.get(URL.format(uuid.uuid4()), headers=(await login_as(cf2, fin)).headers)).status_code == 404
    adv_user = await make_user(db, role="advertiser", scope=("ORGANIZATION", po1.org_id), mfa=True)
    ca = client_factory()
    assert (await ca.get(URL.format(cid), headers=(await login_as(ca, adv_user)).headers)).status_code == 403

    # estorno integral (admin) da contratação inicial ⇒ termo cancelado e estorno visível
    await _refund(db, po1, cid, mail, "decisão da diretoria", override_amount_cents=po1.amount_cents)
    r = await cf.get(URL.format(cid), headers=(await login_as(cf, fin)).headers)
    ini = {t["kind"]: t for t in r.json()["terms"]}["INITIAL"]
    assert ini["status"] == "CANCELLED" and ini["commission_cents"] == 0 and ini["clawback_cents"] > 0
    assert {t["kind"]: t for t in r.json()["terms"]}["RENEWAL"]["status"] == "ACTIVE"


async def test_terms_report_uses_fixed_number_of_queries(db, client_factory):
    cid, screens, ests, old_end, new_end, po1, po2 = await _renewed_campaign(db, client_factory())
    await _accrue_first_days(db, cid, screens, old_end)
    stmts: list[str] = []

    def _count(conn, cursor, statement, *a):
        stmts.append(statement)

    eng = db.bind.sync_engine if hasattr(db.bind, "sync_engine") else db.get_bind()
    event.listen(eng, "before_cursor_execute", _count)
    try:
        full = await commission.campaign_terms_report(db, cid)
        n_full = len(stmts)
        stmts.clear()
        scoped = await commission.campaign_terms_report(db, cid, establishment_ids={ests[0].id})
        n_scoped = len(stmts)
    finally:
        event.remove(eng, "before_cursor_execute", _count)
    assert len(full["terms"]) == 2 and len(scoped["terms"]) == 2
    assert n_full <= 3 and n_scoped <= 3, (n_full, n_scoped)  # termos + acúmulos + liberações (sem N+1)
