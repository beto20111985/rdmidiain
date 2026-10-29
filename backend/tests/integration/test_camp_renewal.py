"""Renovação de campanha (ETAPA2 #509–#517, #774–#776; P001 §54) com W-INV e W-FIN REAIS.

Decisão do coordenador (a confirmar pelo proprietário — "renovação sem reserva prioritária"): a proposta
(manual ou automática D-5) NÃO reserva inventário; a reserva de 2 h nasce no checkout da renovação
(``hold`` real do W-INV); reserva vencida libera; pagamento tardio recaptura; sem capacidade ⇒ erro claro.
Pagamento aplica a extensão SEM reaprovação e sem interrupção; não paga ⇒ FINALIZADA às 23:59:59 locais."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text

from app.models.campaigns import CampaignRevision, CampaignStatusHistory
from app.models.financial import PaymentOrder
from app.models.inventory import InventoryHold
from app.models.pricing import Order
from app.modules.campaigns import checkout as CK
from app.modules.campaigns import renewals as RN
from app.modules.campaigns import service as S
from app.modules.pricing.service import period_end
from tests.camp_real import (
    age_holds,
    approval,
    camp,
    checkout_and_pay,
    create_campaign,
    holds,
    make_active,
    manifest_campaigns,
    pay_order,
    set_payer,
    today_local,
)
from tests.camp_support import future_monday, make_media
from tests.factories import login_as, make_org, make_user
from tests.fin_helpers import fin_env  # noqa: F401  (fixture do W-FIN: provedor falso + segredo de webhook)
from tests.integration.test_camp_lifecycle import world

pytestmark = pytest.mark.usefixtures("fin_env")

SP = timezone(timedelta(hours=-3))


def H(s, key=None):
    return {**s.headers, "Idempotency-Key": key or f"key-{uuid.uuid4().hex}"}


async def monthly_live(db, client):
    t, org, ests, screens, s = await world(db, client)
    await set_payer(db, org.id)
    await approval(db, "AUTO")
    cid = await create_campaign(client, s, db, org, start=await today_local(db), period="MENSAL")
    await checkout_and_pay(db, client, s, cid)
    await make_active(db, cid, screens[0].id)
    return t, org, ests, screens, s, cid


async def test_auto_renew_padrao_e_avulso_nao_renova(db, client):
    t, org, ests, screens, s = await world(db, client)
    start = future_monday().isoformat()

    async def mk(p, **kw):
        return await client.post("/api/v1/campaigns", headers=s.headers, json={
            "name": f"C {p}", "period_type": p, "start_date": start, "period_days": 7 if p == "AVULSO" else None, **kw})

    r = await mk("MENSAL")
    assert r.json()["auto_renew"] is True
    assert (await mk("SEMESTRAL")).json()["auto_renew"] is False
    r = await mk("AVULSO")
    assert r.json()["auto_renew"] is False
    cid = r.json()["id"]
    r2 = await client.put(f"/api/v1/campaigns/{cid}/auto-renew", headers=s.headers, json={"auto_renew": True})
    assert r2.status_code == 400 and r2.json()["error"]["code"] == "RENEWAL_NOT_RECURRING"
    r4 = await client.post(f"/api/v1/campaigns/{cid}/renew", headers=s.headers, json={"periods": 1})
    assert r4.status_code == 409 and r4.json()["error"]["code"] == "RENEWAL_NOT_ALLOWED"


async def test_proposta_sem_reserva_checkout_2h_pagamento_estende_sem_reaprovacao(db, client):
    t, org, ests, screens, s, cid = await monthly_live(db, client)
    c = await camp(db, cid)
    old_end, live_before, prev_price = c.end_date, c.live_revision_id, c.contracted_price_cents
    old_hold = (await holds(db, cid))[0].id
    end_utc = await S.campaign_end_utc(db, c)
    # D-5 automática: PROPOSTA + aviso, SEM reserva de inventário
    out = await RN.generate_auto_renewals(db, now=end_utc - timedelta(days=4))
    await db.commit()
    assert out["created"] == [cid]
    assert len(await holds(db, cid)) == 1  # só a contratação vigente
    n = (await db.execute(text("SELECT count(*) FROM notifications WHERE event_type = 'campaign.renewal_available' "
                               "AND org_id = :o"), {"o": org.id})).scalar_one()
    assert n >= 1
    # reexecução não duplica; POST /renew devolve a mesma proposta (200)
    assert (await RN.generate_auto_renewals(db, now=end_utc - timedelta(days=3)))["created"] == []
    r = await client.post(f"/api/v1/campaigns/{cid}/renew", headers=s.headers, json={"periods": 1})
    assert r.status_code == 200 and r.json()["stage"] == "PROPOSTA"
    new_end = period_end(old_end + timedelta(days=1), "MENSAL")
    assert r.json()["new_end_date"] == new_end.isoformat()
    r = await client.post(f"/api/v1/campaigns/{cid}/renew", headers=s.headers, json={"periods": 2})
    assert r.status_code == 409 and r.json()["error"]["code"] == "RENEWAL_ALREADY_PENDING"
    # checkout da renovação: reserva REAL de 2 h + pedido + cobrança
    key = f"renew-{uuid.uuid4().hex}"
    r = await client.post(f"/api/v1/campaigns/{cid}/renew/checkout", headers=H(s, key), json={})
    assert r.status_code == 201, r.text
    co = r.json()
    hs = await holds(db, cid)
    h = next(x for x in hs if x.id != old_hold)
    assert h.status == "HELD" and h.hold_expires_at - h.created_at == timedelta(hours=2)
    assert (await db.get(Order, uuid.UUID(co["order_id"]))).expires_at == h.hold_expires_at
    # idempotência: mesma chave → mesmo pedido; outra chave com a cobrança aberta → a mesma cobrança
    r2 = await client.post(f"/api/v1/campaigns/{cid}/renew/checkout", headers=H(s, key), json={})
    assert r2.status_code == 200 and r2.json()["order_id"] == co["order_id"]
    r3 = await client.post(f"/api/v1/campaigns/{cid}/renew/checkout", headers=H(s), json={})
    assert r3.status_code == 200 and r3.json()["order_id"] == co["order_id"]
    assert len(await holds(db, cid)) == 2
    g = (await client.get(f"/api/v1/campaigns/{cid}/renewal", headers=s.headers)).json()
    assert g["stage"] == "AGUARDANDO_PAGAMENTO" and g["order"]["order_id"] == co["order_id"]
    # pagamento: aplica (sem EM_ANALISE), mantém ATIVA e tocando
    assert await pay_order(db, client, co["order_id"]) == "PROCESSED"
    c = await camp(db, cid)
    assert c.status == "ATIVA" and c.end_date == new_end and c.live_revision_id != live_before
    assert c.contracted_price_cents == prev_price + co["total_cents"]
    assert {x.status for x in await holds(db, cid)} == {"CONVERTED"}
    hist = (await db.execute(select(CampaignStatusHistory.to_status).where(
        CampaignStatusHistory.campaign_id == c.id))).scalars().all()
    assert "EM_ANALISE" not in hist
    spec = [x for x in await S.playable_campaign_specs(db) if x.campaign_id == c.id][0]
    assert set(spec.hold_ids) == {old_hold, h.id}
    assert cid in await manifest_campaigns(db, screens[0].id)
    # vence no NOVO fim (23:59:59 local), não no antigo
    assert await S.finalize_expired(db, now=datetime(old_end.year, old_end.month, old_end.day, 23, 59, 59,
                                                     tzinfo=SP) + timedelta(seconds=1)) == []


async def test_reserva_da_renovacao_expira_libera_e_pagamento_tardio_recaptura(db, client):
    t, org, ests, screens, s, cid = await monthly_live(db, client)
    await client.post(f"/api/v1/campaigns/{cid}/renew", headers=s.headers, json={"periods": 1})
    co = (await client.post(f"/api/v1/campaigns/{cid}/renew/checkout", headers=H(s), json={})).json()
    order_id = uuid.UUID(co["order_id"])
    # > 2 h: sweeper real expira a reserva; o pedido expira junto; a PROPOSTA continua aberta
    assert await age_holds(db, cid) == 1
    o = await db.get(Order, order_id)
    assert await CK.expire_overdue_orders(db, now=o.expires_at + timedelta(seconds=1)) == [order_id]
    await db.commit()
    assert (await db.get(Order, order_id)).status == "EXPIRADO"
    assert sorted(x.status for x in await holds(db, cid)) == ["CONVERTED", "EXPIRED"]
    g = (await client.get(f"/api/v1/campaigns/{cid}/renewal", headers=s.headers)).json()
    assert g["stage"] == "PROPOSTA"
    # pagamento tardio da cobrança vencida: o W-INV recaptura (capacidade livre) e a renovação é aplicada
    assert await pay_order(db, client, order_id) == "PROCESSED"
    c = await camp(db, cid)
    assert c.end_date.isoformat() == co["new_end_date"] and c.status == "ATIVA"
    late = [x for x in await holds(db, cid) if (x.details or {}).get("recaptured_from")]
    assert len(late) == 1 and late[0].status == "CONVERTED"


async def _competitor(db, client, *, start, end, freq, duration):
    """Outro anunciante (sem perfil de concorrência) nas mesmas telas e no mesmo período."""
    org_b = await make_org(db)
    await set_payer(db, org_b.id)
    ub = await make_user(db, role="advertiser", scope=("ORGANIZATION", org_b.id))
    sb = await login_as(client, ub)
    r = await client.post("/api/v1/campaigns", headers=sb.headers, json={
        "name": "Concorrente", "period_type": "AVULSO", "start_date": start.isoformat(), "end_date": end.isoformat(),
        "duration_s": duration, "freq_kind": "PER_HOUR", "freq_value": freq,
        "targets": [{"type": "CITY", "value": "GO:Goiânia"}]})
    assert r.status_code == 201, r.text
    cid_b = r.json()["id"]
    m = await make_media(db, org_b, duration_ms=duration * 1000)
    cr = (await client.post("/api/v1/creatives", headers=sb.headers,
                            json={"name": "Peça B", "media_file_id": str(m.id), "duration_seconds": duration})).json()
    assert (await client.post(f"/api/v1/campaigns/{cid_b}/creatives", headers=sb.headers,
                              json={"creative_id": cr["id"]})).status_code == 201
    return sb, cid_b


async def test_concorrencia_checkout_da_renovacao_x_outro_anunciante(db, client, client_factory):
    t, org, ests, screens, s, cid = await monthly_live(db, client)
    p = (await client.post(f"/api/v1/campaigns/{cid}/renew", headers=s.headers, json={"periods": 1})).json()
    ext_start = datetime.fromisoformat(p["extension_start"]).date()
    new_end = datetime.fromisoformat(p["new_end_date"]).date()
    # 120 s × 15/h = 60 unidades/h (capacidade inteira do 2+2): só um dos dois cabe
    sb, cid_b = await _competitor(db, client, start=ext_start, end=new_end, freq=15, duration=120)
    a, b = client_factory(), client_factory()
    ra, rb = await asyncio.gather(
        a.post(f"/api/v1/campaigns/{cid}/renew/checkout", headers=H(s), json={}),
        b.post(f"/api/v1/campaigns/{cid_b}/checkout", headers=H(sb), json={}))
    assert sorted([ra.status_code, rb.status_code]) == [201, 409], (ra.text, rb.text)
    loser = ra if ra.status_code == 409 else rb
    assert loser.json()["error"]["code"] == "INVENTORY_UNAVAILABLE"
    if ra.status_code == 409:
        # renovação sem capacidade: erro claro e a proposta continua aberta
        g = (await client.get(f"/api/v1/campaigns/{cid}/renewal", headers=s.headers)).json()
        assert g["stage"] == "PROPOSTA"
    else:
        assert (await camp(db, cid_b)).status == "RASCUNHO"
    n_held = (await db.execute(select(func.count()).select_from(InventoryHold).where(
        InventoryHold.status == "HELD"))).scalar_one()
    assert n_held == 1  # nunca overbooking


async def test_pagamento_tardio_sem_capacidade_recusa_renovacao(db, client):
    t, org, ests, screens, s, cid = await monthly_live(db, client)
    p = (await client.post(f"/api/v1/campaigns/{cid}/renew", headers=s.headers, json={"periods": 1})).json()
    co = (await client.post(f"/api/v1/campaigns/{cid}/renew/checkout", headers=H(s), json={})).json()
    await age_holds(db, cid)
    ext_start = datetime.fromisoformat(p["extension_start"]).date()
    new_end = datetime.fromisoformat(p["new_end_date"]).date()
    sb, cid_b = await _competitor(db, client, start=ext_start, end=new_end, freq=15, duration=120)
    rb = await client.post(f"/api/v1/campaigns/{cid_b}/checkout", headers=H(sb), json={})
    assert rb.status_code == 201, rb.text  # a reserva da renovação venceu: o outro anunciante leva
    c0 = await camp(db, cid)
    assert await pay_order(db, client, co["order_id"]) == "PROCESSED"
    c = await camp(db, cid)
    assert c.end_date == c0.end_date and c.payment_issue == "RENEWAL_PAID_WITHOUT_INVENTORY"
    rev = (await db.execute(select(CampaignRevision).where(CampaignRevision.campaign_id == c.id,
                                                           CampaignRevision.kind == "RENEWAL"))).scalar_one()
    assert rev.status == "REJEITADA" and rev.change_summary["requires_refund_review"] is True


async def test_nao_paga_finaliza_no_vencimento_local_e_pagamento_posterior_nao_reativa(db, client):
    t, org, ests, screens, s, cid = await monthly_live(db, client)
    c = await camp(db, cid)
    old_end = c.end_date
    await client.post(f"/api/v1/campaigns/{cid}/renew", headers=s.headers, json={"periods": 1})
    co = (await client.post(f"/api/v1/campaigns/{cid}/renew/checkout", headers=H(s), json={})).json()
    end_local = datetime(old_end.year, old_end.month, old_end.day, 23, 59, 59, tzinfo=SP)
    assert await S.finalize_expired(db, now=end_local) == []  # 23:59:59 exato ainda vale
    assert await S.finalize_expired(db, now=end_local + timedelta(seconds=1)) == [c.id]
    await db.commit()
    c = await camp(db, cid)
    assert c.status == "FINALIZADA" and c.pending_revision_id is None and c.end_date == old_end
    assert (await db.get(Order, uuid.UUID(co["order_id"]))).status == "EXPIRADO"
    rev = (await db.execute(select(CampaignRevision).where(CampaignRevision.campaign_id == c.id,
                                                           CampaignRevision.kind == "RENEWAL"))).scalar_one()
    assert rev.status == "EXPIRADA"
    # pagamento depois do vencimento: o callback recusa (divergência → estorno pelo W-FIN); nada reativa
    st = await pay_order(db, client, co["order_id"])
    assert st in ("PROCESSED", "FAILED", "DIVERGENT", "ERROR")
    c = await camp(db, cid)
    assert c.status == "FINALIZADA" and c.end_date == old_end


async def test_renovacao_de_outra_org_404_e_sem_permissao(db, client):
    t, org, s_unused, screens, s, cid = await monthly_live(db, client)
    other = await make_org(db)
    uo = await make_user(db, role="advertiser", scope=("ORGANIZATION", other.id))
    so = await login_as(client, uo)
    assert (await client.post(f"/api/v1/campaigns/{cid}/renew", headers=so.headers,
                              json={"periods": 1})).status_code == 404
    assert (await client.post(f"/api/v1/campaigns/{cid}/renew", headers=s.headers,
                              json={"periods": 1, "amount_cents": 1})).status_code == 422
    await client.post(f"/api/v1/campaigns/{cid}/renew", headers=s.headers, json={"periods": 1})
    r = await client.post(f"/api/v1/campaigns/{cid}/renew/checkout", headers=s.headers, json={})
    assert r.status_code == 400 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert (await client.post(f"/api/v1/campaigns/{cid}/renew/checkout", headers=H(so), json={})).status_code == 404


async def test_telas_contratadas_visao_publica_e_rd(db, client):
    t, org, ests, screens, s, cid = await monthly_live(db, client)
    r = await client.get(f"/api/v1/campaigns/{cid}/screens", headers=s.headers)
    assert r.status_code == 200 and r.json()["source"] == "CONTRATADAS" and r.json()["total"] == 2
    assert all(set(i) == {"friendly_id", "state", "orientation", "letterbox"} for i in r.json()["items"])
    admin = await make_user(db, role="admin", mfa=True)
    sa = await login_as(client, admin)
    r = await client.get(f"/api/v1/campaigns/{cid}/screens", headers=sa.headers)
    assert {i["establishment_id"] for i in r.json()["items"]} == {str(sc.establishment_id) for sc in screens}


async def test_cancelar_paga_com_renovacao_pendente_cancela_cobranca_libera_reserva_e_nao_reativa(db, client):
    """REQ-CAN-001 (regressão; achado D-02 do revisor final): cancelar a campanha PAGA com a renovação
    aguardando pagamento cancela o pedido e a cobrança da renovação, libera a reserva de 2 h, e o PIX
    pago depois (tardio) não reativa a campanha nem estende o período."""
    t, org, ests, screens, s, cid = await monthly_live(db, client)
    old = {h.id for h in await holds(db, cid)}
    r = await client.post(f"/api/v1/campaigns/{cid}/renew", headers=s.headers, json={"periods": 1})
    assert r.status_code in (200, 201), r.text
    r = await client.post(f"/api/v1/campaigns/{cid}/renew/checkout", headers=H(s), json={})
    assert r.status_code == 201, r.text
    oid = uuid.UUID(r.json()["order_id"])
    new_holds = [h for h in await holds(db, cid) if h.id not in old]
    assert len(new_holds) == 1 and new_holds[0].status == "HELD"

    r = await client.post(f"/api/v1/campaigns/{cid}/cancel", headers=H(s), json={"reason": "cancelamento com renovacao pendente"})
    assert r.status_code == 200, r.text
    db.expire_all()
    c = await camp(db, cid)
    assert c.status == "CANCELADA", c.status
    o = (await db.execute(select(Order).where(Order.id == oid).execution_options(populate_existing=True))).scalar_one()
    assert o.status == "CANCELADO", o.status
    po = (await db.execute(select(PaymentOrder).where(PaymentOrder.id == o.payment_order_id)
                           .execution_options(populate_existing=True))).scalar_one()
    assert po.status == "CANCELLED", po.status
    st = {h.id: h.status for h in await holds(db, cid)}
    assert all(v not in ("HELD", "CONVERTED") for v in st.values()), st

    end_before = c.end_date
    res = await pay_order(db, client, str(oid))
    db.expire_all()
    c2 = await camp(db, cid)
    assert c2.status == "CANCELADA" and c2.end_date == end_before, (res, c2.status, c2.end_date)
