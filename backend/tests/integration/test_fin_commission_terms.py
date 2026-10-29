"""Comissão POR PAGAMENTO (correção (h)) e peso POR TELA (correção (i)) com os domínios REAIS:
W-CAMP (checkout, renovação, alteração paga/gratuita, aprovação) + W-INV (reserva/BOOKING,
``contracted_exhibitions``) + W-FIN (cobrança, webhook assinado, termos, apropriação, estorno).

- renovação paga ⇒ termo RENEWAL com pool = 5 % DESSA cobrança; PoP do período novo apropria
  no termo novo; execução integral do período ⇒ Σ = pool (menos o piso por tela);
- alteração paga que inclui tela ⇒ termo CHANGE (do pagamento ao fim); o estabelecimento novo ganha;
- alteração gratuita que troca telas ⇒ nenhum termo novo (pool inalterado) e a distribuição dos
  dias seguintes segue as telas ATUAIS;
- estorno de UMA cobrança afeta só o seu termo;
- Σ apropriado ≤ 5 % de CADA pagamento;
- tela premium 1500 × padrão 1000 no mesmo estabelecimento-peso ⇒ 3 : 2.
"""
import uuid
from datetime import timedelta
from fractions import Fraction

import pytest
from sqlalchemy import func, select, update

from app.models.financial import CommissionAccrual, CommissionCampaignScreen, CommissionDayPlan, PaymentOrder
from app.models.pricing import Order
from app.models.screens import Screen
from app.modules.financial import approvals, commission, refunds
from tests.camp_real import (
    approval,
    camp,
    checkout_and_pay,
    create_campaign,
    make_active,
    pay_order,
    set_payer,
    today_local,
)
from tests.camp_support import make_est, make_screen
from tests.factories import login_as, make_user
from tests.fin_helpers import approve_svc, ctx_for, fin_env, make_admin_masters, pop  # noqa: F401
from tests.integration.test_camp_lifecycle import world

pytestmark = pytest.mark.usefixtures("fin_env")

UC = commission.UC


def H(s):
    return {**s.headers, "Idempotency-Key": f"key-{uuid.uuid4().hex}"}


async def _po_of_order(db, order_id) -> PaymentOrder:
    o = (await db.execute(select(Order).where(Order.id == uuid.UUID(str(order_id)))
                          .execution_options(populate_existing=True))).scalar_one()
    return await db.get(PaymentOrder, o.payment_order_id)


async def _accrued(db, terms_id, screen_id=None) -> int:
    q = select(func.coalesce(func.sum(CommissionAccrual.amount_ucents - CommissionAccrual.reversed_ucents), 0)) \
        .where(CommissionAccrual.terms_id == terms_id)
    if screen_id is not None:
        q = q.where(CommissionAccrual.screen_id == screen_id)
    return int((await db.execute(q)).scalar_one())


async def _execute_all(db, cid, plan, d0, d1) -> None:
    """PoP válido integral (= contratado) de todas as telas nos dias [d0, d1]."""
    for sid, days in plan.items():
        evs = [e for d, n in sorted(days.items()) if d0 <= d <= d1 for e in pop(cid, sid, d, n)]
        if evs:
            await commission.accrue_commission_for_events(db, evs)
    await db.commit()


async def _assert_pool_cap(db, cid) -> None:
    """Σ apropriado ≤ pool (5 %) de CADA cobrança paga."""
    for t in await commission.terms_of(db, cid):
        assert t.pool_cents == t.eligible_revenue_cents * 500 // 10_000
        assert await _accrued(db, t.id) <= t.pool_cents * UC, (t.kind, t.pool_cents)


async def _renewed_campaign(db, client):
    t, org, ests, screens, s = await world(db, client)
    await set_payer(db, org.id)
    await approval(db, "AUTO")
    cid = await create_campaign(client, s, db, org, start=await today_local(db), period="MENSAL")
    first = await checkout_and_pay(db, client, s, cid)
    await make_active(db, cid, screens[0].id)
    old_end = (await camp(db, cid)).end_date
    r = await client.post(f"/api/v1/campaigns/{cid}/renew", headers=s.headers, json={"periods": 1})
    assert r.status_code == 201, r.text
    r = await client.post(f"/api/v1/campaigns/{cid}/renew/checkout", headers=H(s), json={})
    assert r.status_code == 201, r.text
    assert await pay_order(db, client, r.json()["order_id"]) == "PROCESSED"
    c = await camp(db, cid)
    assert c.status == "ATIVA" and c.end_date > old_end
    po1 = await _po_of_order(db, first["order_id"])
    po2 = await _po_of_order(db, r.json()["order_id"])
    return uuid.UUID(cid), screens, ests, old_end, c.end_date, po1, po2


async def test_renewal_payment_creates_own_terms_and_new_period_pop_earns_from_it(db, client):
    cid, screens, ests, old_end, new_end, po1, po2 = await _renewed_campaign(db, client)
    t1 = await commission.terms_for_payment(db, cid, po1.id)
    t2 = await commission.terms_for_payment(db, cid, po2.id)
    assert (t1.kind, t2.kind) == ("INITIAL", "RENEWAL")
    assert t1.end_date == old_end and (t2.start_date, t2.end_date) == (old_end + timedelta(days=1), new_end)
    assert t2.eligible_revenue_cents == po2.eligible_revenue_cents and t2.pool_cents == po2.eligible_revenue_cents * 500 // 10_000
    plan = await commission.contracted_plan(db, cid)
    assert any(d > old_end for days in plan.values() for d in days)  # o BOOKING da renovação está no W-INV
    # PoP do período NOVO: só o termo da renovação apropria
    sid = screens[0].id
    day_new = min(d for d in plan[sid] if d > old_end)
    r = await commission.accrue_commission_for_events(db, pop(cid, sid, day_new, plan[sid][day_new]))
    await db.commit()
    assert r["counted"] == plan[sid][day_new]
    rows = (await db.execute(select(CommissionAccrual.terms_id, CommissionAccrual.local_day))).all()
    assert rows == [(t2.id, day_new)]
    # PoP do período ANTIGO: só o termo inicial
    day_old = min(plan[sid])
    await commission.accrue_commission_for_events(db, pop(cid, sid, day_old, plan[sid][day_old]))
    await db.commit()
    assert await _accrued(db, t1.id) > 0 and {r[0] for r in (await db.execute(
        select(CommissionAccrual.terms_id).where(CommissionAccrual.local_day == day_old))).all()} == {t1.id}
    # execução INTEGRAL do período renovado ⇒ Σ do termo = pool da renovação (piso por tela)
    await _execute_all(db, cid, plan, t2.start_date, t2.end_date)
    total2 = await _accrued(db, t2.id)
    assert t2.pool_cents * UC - len(plan) <= total2 <= t2.pool_cents * UC
    await _assert_pool_cap(db, cid)


async def _refund(db, po, cid, mail, reason, **kw):
    a, b = await make_admin_masters(db)
    fin = await make_user(db, role="financial")
    ref = await refunds.request_refund(db, await ctx_for(db, fin), po.id, cid, reason, **kw)
    await db.commit()
    req = await approvals.create_request(db, await ctx_for(db, a), "REFUND_PIX_OUT",
                                         {"refund_id": str(ref.id), "await_provider_refund": True}, reason)
    await db.commit()
    await approve_svc(db, b, req, mail)
    db.expunge_all()
    return ref


async def test_refund_of_renewal_payment_affects_only_its_terms(db, client, mail):
    cid, screens, ests, old_end, new_end, po1, po2 = await _renewed_campaign(db, client)
    plan = await commission.contracted_plan(db, cid)
    sid = screens[0].id
    d_old = min(plan[sid])
    await commission.accrue_commission_for_events(db, pop(cid, sid, d_old, plan[sid][d_old]))
    await db.commit()
    t1 = await commission.terms_for_payment(db, cid, po1.id)
    before_t1, pool_t1 = await _accrued(db, t1.id), t1.pool_cents
    assert before_t1 > 0
    # estorno integral da RENOVAÇÃO (período ainda não começou) com dupla aprovação
    ref = await _refund(db, po2, cid, mail, "renovação desistida antes do início")
    assert ref.policy == "FULL_BEFORE_START" and ref.media_refund_cents == po2.eligible_revenue_cents
    t1 = await commission.terms_for_payment(db, cid, po1.id)
    t2 = await commission.terms_for_payment(db, cid, po2.id)
    assert (t1.status, t2.status) == ("ACTIVE", "CANCELLED") and t2.pool_cents == 0
    assert (await _accrued(db, t1.id), t1.pool_cents) == (before_t1, pool_t1)  # termo inicial intacto
    # PoP do período renovado não gera mais comissão; o período inicial continua gerando
    d_new = min(d for d in plan[sid] if d > old_end)
    assert (await commission.accrue_commission_for_events(db, pop(cid, sid, d_new, 5)))["counted"] == 0
    d_old2 = min(d for d in plan[sid] if d > d_old)
    r = await commission.accrue_commission_for_events(db, pop(cid, sid, d_old2, 5))
    await db.commit()
    assert r["counted"] == 5 and await _accrued(db, t1.id) > before_t1


async def test_override_refund_of_initial_payment_reverts_only_initial_terms(db, client, mail):
    cid, screens, ests, old_end, new_end, po1, po2 = await _renewed_campaign(db, client)
    plan = await commission.contracted_plan(db, cid)
    sid = screens[0].id
    d_old = min(plan[sid])
    await commission.accrue_commission_for_events(db, pop(cid, sid, d_old, plan[sid][d_old]))
    await db.commit()
    t2 = await commission.terms_for_payment(db, cid, po2.id)
    pool_t2 = t2.pool_cents
    # decisão do admin: estorno INTEGRAL da contratação inicial (dupla aprovação) ⇒ comissão executada estornada
    ref = await _refund(db, po1, cid, mail, "decisão da diretoria", override_amount_cents=po1.amount_cents)
    assert ref.policy == "ADMIN_OVERRIDE"
    t1 = await commission.terms_for_payment(db, cid, po1.id)
    t2 = await commission.terms_for_payment(db, cid, po2.id)
    assert (t1.status, t2.status) == ("CANCELLED", "ACTIVE")
    assert await _accrued(db, t1.id) == 0                      # pendente revertida só no termo estornado
    assert t2.pool_cents == pool_t2                            # pool da renovação intacto
    # a renovação continua apropriando no seu período
    d_new = min(d for d in plan[sid] if d > old_end)
    r = await commission.accrue_commission_for_events(db, pop(cid, sid, d_new, plan[sid][d_new]))
    await db.commit()
    assert r["counted"] == plan[sid][d_new] and await _accrued(db, t2.id) > 0
    assert {x for (x,) in (await db.execute(select(CommissionAccrual.terms_id).where(
        CommissionAccrual.local_day == d_new))).all()} == {t2.id}


async def test_paid_change_adding_screen_creates_change_terms_and_new_establishment_earns(db, client):
    t, org, ests, screens, s = await world(db, client)
    await set_payer(db, org.id)
    await approval(db, "AUTO")
    today = await today_local(db)
    cid = await create_campaign(client, s, db, org, start=today, days=10)
    first = await checkout_and_pay(db, client, s, cid)
    await make_active(db, cid, screens[0].id)
    ana = await make_est(db, category=t.supermercado, segments=[t.alimentos], city="Anápolis", state="GO")
    s3 = await make_screen(db, ana)
    r = await client.post(f"/api/v1/campaigns/{cid}/changes", headers=H(s),
                          json={"targets": [{"type": "STATE", "value": "GO"}]})
    assert r.status_code == 201, r.text
    assert r.json()["diff_cents"] > 0 and r.json()["order"] is not None
    assert await pay_order(db, client, r.json()["order"]["order_id"]) == "PROCESSED"
    c = await camp(db, cid)
    if c.pending_revision_id is not None:  # aprovação da alteração (versão nova passa a valer)
        sa = await login_as(client, await make_user(db, role="admin", mfa=True))
        ra = await client.post(f"/api/v1/campaigns/{cid}/approve", headers=sa.headers, json={})
        assert ra.status_code == 200, ra.text
    cid = uuid.UUID(cid)
    po1 = await _po_of_order(db, first["order_id"])
    po2 = await _po_of_order(db, r.json()["order"]["order_id"])
    t1 = await commission.terms_for_payment(db, cid, po1.id)
    t2 = await commission.terms_for_payment(db, cid, po2.id)
    assert (t1.kind, t2.kind) == ("INITIAL", "CHANGE")
    assert t2.start_date == today and t2.end_date == t1.end_date  # do pagamento ao fim (não retroage)
    assert t2.pool_cents == r.json()["diff_cents"] * 500 // 10_000
    plan = await commission.contracted_plan(db, cid)
    assert s3.id in plan  # a tela incluída está no BOOKING vigente
    day = min(d for d in plan[s3.id] if d > today)
    r2 = await commission.accrue_commission_for_events(db, pop(cid, s3.id, day, plan[s3.id][day]))
    await db.commit()
    assert r2["counted"] == plan[s3.id][day]
    rows = (await db.execute(select(CommissionAccrual.terms_id, CommissionAccrual.establishment_id, CommissionAccrual.amount_ucents)
                             .where(CommissionAccrual.screen_id == s3.id))).all()
    assert {r_[0] for r_ in rows} == {t1.id, t2.id}            # termos sobrepostos pagam cada um o seu pool
    assert all(r_[1] == ana.id and r_[2] > 0 for r_ in rows)   # o estabelecimento NOVO ganha
    await _execute_all(db, cid, plan, day, t1.end_date)
    await _assert_pool_cap(db, cid)


async def test_free_change_swapping_screens_keeps_pool_and_follows_current_screens(db, client):
    t, org, ests, screens, s = await world(db, client)
    ana = [await make_est(db, category=t.supermercado, segments=[t.alimentos], city="Anápolis", state="GO")
           for _ in range(2)]
    ana_screens = [await make_screen(db, e) for e in ana]
    await set_payer(db, org.id)
    await approval(db, "AUTO")
    today = await today_local(db)
    cid = await create_campaign(client, s, db, org, start=today, days=10)
    first = await checkout_and_pay(db, client, s, cid)
    await make_active(db, cid, screens[0].id)
    cid_u = uuid.UUID(cid)
    plan0 = await commission.contracted_plan(db, cid_u)
    assert set(plan0) == {sc.id for sc in screens}
    for sc in screens:  # hoje: telas originais (plano do dia congelado)
        await commission.accrue_commission_for_events(db, pop(cid_u, sc.id, today, plan0[sc.id][today]))
    await db.commit()
    r = await client.post(f"/api/v1/campaigns/{cid}/changes", headers=H(s),
                          json={"targets": [{"type": "CITY", "value": "GO:Anápolis"}]})
    assert r.status_code == 201, r.text
    assert r.json()["diff_cents"] <= 0 and r.json()["order"] is None  # alteração SEM custo
    assert (await camp(db, cid)).pending_revision_id is None           # aprovação automática aplicada
    (terms,) = await commission.terms_of(db, cid_u)                   # nenhum termo novo
    po1 = await _po_of_order(db, first["order_id"])
    assert terms.payment_order_id == po1.id and terms.pool_cents == po1.eligible_revenue_cents * 500 // 10_000
    plan1 = await commission.contracted_plan(db, cid_u)
    tomorrow = today + timedelta(days=1)
    assert {sid for sid, d in plan1.items() if tomorrow in d} == {sc.id for sc in ana_screens}
    # amanhã: telas antigas não contam; as atuais recebem
    r_old = await commission.accrue_commission_for_events(db, pop(cid_u, screens[0].id, tomorrow, 3))
    assert r_old["counted"] == 0
    for sc in ana_screens:
        await commission.accrue_commission_for_events(db, pop(cid_u, sc.id, tomorrow, plan1[sc.id][tomorrow]))
    await db.commit()
    dp = await db.get(CommissionDayPlan, (terms.id, tomorrow))
    assert set(dp.plan) == {str(sc.id) for sc in ana_screens}
    for sc, est in zip(ana_screens, ana):
        assert await _accrued(db, terms.id, sc.id) > 0
        assert (await db.execute(select(func.count()).select_from(CommissionAccrual).where(
            CommissionAccrual.screen_id == sc.id, CommissionAccrual.establishment_id == est.id))).scalar_one() == 1
    # o dia de hoje continua com as telas originais (plano congelado na 1ª apropriação)
    assert set((await db.get(CommissionDayPlan, (terms.id, today))).plan) == {str(sc.id) for sc in screens}
    await _assert_pool_cap(db, cid_u)


async def test_screen_weight_premium_1500_vs_standard_1000_is_3_to_2(db, client):
    t, org, ests, screens, s = await world(db, client)  # 2 estabelecimentos de peso 1000, 1 tela cada
    premium, standard = screens
    await db.execute(update(Screen).where(Screen.id == premium.id).values(weight_milli=1500))
    await db.commit()
    await set_payer(db, org.id)
    await approval(db, "AUTO")
    today = await today_local(db)
    cid = uuid.UUID(await create_campaign(client, s, db, org, start=today, days=7))
    await checkout_and_pay(db, client, s, str(cid))
    plan = await commission.contracted_plan(db, cid)
    day = today
    assert plan[premium.id][day] == plan[standard.id][day] > 0  # mesma entrega contratada
    for sc in (premium, standard):
        await commission.accrue_commission_for_events(db, pop(cid, sc.id, day, plan[sc.id][day]))
    await db.commit()
    (terms,) = await commission.terms_of(db, cid)
    rows = {r.screen_id: r for r in (await db.execute(select(CommissionCampaignScreen)
                                                      .where(CommissionCampaignScreen.terms_id == terms.id))).scalars()}
    # peso efetivo = peso do estabelecimento × peso da tela / 1000 (inteiros), congelado no termo
    assert (rows[premium.id].establishment_weight_milli, rows[premium.id].screen_weight_milli,
            rows[premium.id].weight_milli) == (1000, 1500, 1500)
    assert (rows[standard.id].establishment_weight_milli, rows[standard.id].screen_weight_milli,
            rows[standard.id].weight_milli) == (1000, 1000, 1000)
    a, b = await _accrued(db, terms.id, premium.id), await _accrued(db, terms.id, standard.id)
    dp = await db.get(CommissionDayPlan, (terms.id, day))
    budget = Fraction(terms.pool_cents * UC * dp.e_day, terms.expected_total)
    assert (a, b) == (int(budget * 3 / 5), int(budget * 2 / 5))  # 3 : 2 dentro do orçamento do dia
    assert abs(a * 2 - b * 3) <= 3 and a + b <= budget
    await _assert_pool_cap(db, cid)


async def test_establishment_weight_times_screen_weight(db, client):
    """Peso do estabelecimento 2000 × tela 500 ⇒ efetivo 1000 (igual à tela padrão do outro)."""
    from app.models.establishments import Establishment

    t, org, ests, screens, s = await world(db, client)
    await db.execute(update(Establishment).where(Establishment.id == ests[0].id).values(weight_milli=2000))
    await db.execute(update(Screen).where(Screen.id == screens[0].id).values(weight_milli=500))
    await db.commit()
    await set_payer(db, org.id)
    await approval(db, "AUTO")
    today = await today_local(db)
    cid = uuid.UUID(await create_campaign(client, s, db, org, start=today, days=7))
    await checkout_and_pay(db, client, s, str(cid))
    plan = await commission.contracted_plan(db, cid)
    for sc in screens:
        await commission.accrue_commission_for_events(db, pop(cid, sc.id, today, plan[sc.id][today]))
    await db.commit()
    (terms,) = await commission.terms_of(db, cid)
    r0 = await db.get(CommissionCampaignScreen, (terms.id, screens[0].id))
    assert (r0.establishment_weight_milli, r0.screen_weight_milli, r0.weight_milli) == (2000, 500, 1000)
    assert await _accrued(db, terms.id, screens[0].id) == await _accrued(db, terms.id, screens[1].id)
