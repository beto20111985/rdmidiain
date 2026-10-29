"""Alteração pós-aprovação com W-INV e W-FIN REAIS — invariantes do item (f):
(i) campanha aprovada/ativa NUNCA fica sem capacidade BOOKED; (ii) sem contagem dupla na reserva nova
(``replaces_hold_id``); (iii) nenhum hold > 2 h; (iv) rejeição mantém a reserva anterior; (v) sem capacidade
na aprovação ⇒ erro claro + notificação, reserva anterior intacta."""
import uuid

import pytest
from sqlalchemy import select, text

from tests.camp_real import (
    age_holds,
    approval,
    booked,
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
from tests.camp_support import make_est, make_screen
from tests.factories import login_as, make_org, make_user
from tests.fin_helpers import fin_env  # noqa: F401  (fixture do W-FIN: provedor falso + segredo de webhook)
from tests.integration.test_camp_lifecycle import world

pytestmark = pytest.mark.usefixtures("fin_env")


async def live(db, client, *, auto_initial=True):
    t, org, ests, screens, s = await world(db, client)
    await set_payer(db, org.id)
    await approval(db, "AUTO" if auto_initial else "MANUAL")
    cid = await create_campaign(client, s, db, org, start=await today_local(db), days=7)
    await checkout_and_pay(db, client, s, cid)
    await make_active(db, cid, screens[0].id)
    admin = await make_user(db, role="admin", mfa=True)
    sa = await login_as(client, admin)
    return t, org, ests, screens, s, sa, cid


async def test_alteracao_sem_cobranca_manual_aprovada_depois_de_2h_mantem_booked(db, client):
    t, org, ests, screens, s, sa, cid = await live(db, client)
    await approval(db, "MANUAL")
    old = (await holds(db, cid))[0]
    r = await client.post(f"/api/v1/campaigns/{cid}/changes", headers=s.headers, json={"time_bands": [[480, 1200]]})
    assert r.status_code == 201, r.text
    assert r.json()["diff_cents"] < 0 and r.json()["campaign_status"] == "EM_ANALISE"
    # na submissão NADA é reservado; a contratação vigente continua BOOKED e tocando
    assert [h.id for h in await holds(db, cid)] == [old.id]
    assert await booked(db, cid) == {old.id: 2}
    assert cid in await manifest_campaigns(db, screens[0].id)
    await age_holds(db)  # > 2 h depois (sweeper real)
    assert await booked(db, cid) == {old.id: 2}
    r = await client.post(f"/api/v1/campaigns/{cid}/approve", headers=sa.headers, json={})
    # aprovada (o W-INV já publica os manifestos ⇒ pode seguir direto para SINCRONIZANDO)
    assert r.status_code == 200 and r.json()["status"] in ("APROVADA", "SINCRONIZANDO"), r.text
    hs = {h.id: h for h in await holds(db, cid)}
    new = next(h for h in hs.values() if h.id != old.id)
    assert new.status == "CONVERTED" and new.replaces_hold_id == old.id
    assert new.hold_expires_at - new.created_at <= __import__("datetime").timedelta(hours=2)
    assert hs[old.id].status == "CANCELLED" and hs[old.id].release_reason == "SUPERSEDED_BY_REVISION"
    assert await booked(db, cid) == {new.id: 2}
    assert cid in await manifest_campaigns(db, screens[0].id)
    c = await camp(db, cid)
    from app.models.campaigns import CampaignRevision

    assert (await db.get(CampaignRevision, c.live_revision_id)).spec["time_bands"] == [[480, 1200]]


async def test_alteracao_sem_cobranca_auto_converte_na_hora(db, client):
    t, org, ests, screens, s, sa, cid = await live(db, client)
    old = (await holds(db, cid))[0]
    r = await client.post(f"/api/v1/campaigns/{cid}/changes", headers=s.headers, json={"days_mask": 31})
    assert r.status_code == 201 and r.json()["campaign_status"] in ("APROVADA", "SINCRONIZANDO"), r.text
    hs = await holds(db, cid)
    new = next(h for h in hs if h.id != old.id)
    assert new.status == "CONVERTED" and {h.id: h.status for h in hs}[old.id] == "CANCELLED"
    assert await booked(db, cid) == {new.id: 2}
    assert cid in await manifest_campaigns(db, screens[0].id)


async def test_alteracao_paga_convertida_no_pagamento_e_aprovada(db, client):
    t, org, ests, screens, s, sa, cid = await live(db, client)
    await approval(db, "MANUAL")
    ana = await make_est(db, category=t.supermercado, segments=[t.alimentos], city="Anápolis", state="GO")
    s3 = await make_screen(db, ana)
    old = (await holds(db, cid))[0]
    r = await client.post(f"/api/v1/campaigns/{cid}/changes", headers={**s.headers, "Idempotency-Key": "chg-real-0001"},
                          json={"targets": [{"type": "STATE", "value": "GO"}]})
    assert r.status_code == 201 and r.json()["diff_cents"] > 0, r.text
    new = next(h for h in await holds(db, cid) if h.id != old.id)
    assert new.status == "HELD" and new.hold_expires_at - new.created_at == __import__("datetime").timedelta(hours=2)
    assert await pay_order(db, client, r.json()["order"]["order_id"]) == "PROCESSED"
    assert (await camp(db, cid)).status == "EM_ANALISE"
    await age_holds(db)  # análise atrasada > 2 h: a reserva paga (CONVERTED) não se perde
    hs = {h.id: h.status for h in await holds(db, cid)}
    assert hs == {old.id: "CONVERTED", new.id: "CONVERTED"}
    assert cid in await manifest_campaigns(db, screens[0].id)  # versão vigente continua tocando
    assert cid not in await manifest_campaigns(db, s3.id)  # a nova (pendente) ocupa, mas não toca
    r = await client.post(f"/api/v1/campaigns/{cid}/approve", headers=sa.headers, json={})
    assert r.status_code == 200, r.text
    hs = {h.id: h.status for h in await holds(db, cid)}
    assert hs == {old.id: "CANCELLED", new.id: "CONVERTED"}
    assert await booked(db, cid) == {new.id: 3}
    assert cid in await manifest_campaigns(db, s3.id)


async def test_alteracao_rejeitada_mantem_reserva_anterior(db, client):
    t, org, ests, screens, s, sa, cid = await live(db, client)
    await approval(db, "MANUAL")
    old = (await holds(db, cid))[0]
    await client.post(f"/api/v1/campaigns/{cid}/changes", headers=s.headers, json={"time_bands": [[480, 1200]]})
    r = await client.post(f"/api/v1/campaigns/{cid}/reject", headers=sa.headers,
                          json={"reason": "Faixa fora da política comercial"})
    assert r.status_code == 200 and r.json()["status"] == "ATIVA", r.text
    assert [(h.id, h.status) for h in await holds(db, cid)] == [(old.id, "CONVERTED")]
    assert await booked(db, cid) == {old.id: 2}
    # alteração PAGA e rejeitada: a nova é liberada (REFUNDED) e a anterior segue intacta
    ana = await make_est(db, category=t.supermercado, segments=[t.alimentos], city="Anápolis", state="GO")
    await make_screen(db, ana)
    r = await client.post(f"/api/v1/campaigns/{cid}/changes", headers={**s.headers, "Idempotency-Key": "chg-real-0002"},
                          json={"targets": [{"type": "STATE", "value": "GO"}]})
    assert await pay_order(db, client, r.json()["order"]["order_id"]) == "PROCESSED"
    chg_order = r.json()["order"]["order_id"]
    r = await client.post(f"/api/v1/campaigns/{cid}/reject", headers=sa.headers, json={"reason": "Região não aprovada"})
    assert r.status_code == 200 and r.json()["status"] == "ATIVA"
    # reembolso REAL do W-FIN pedido para a cobrança da diferença (dinheiro só sai após dupla aprovação)
    from app.models.financial import Refund
    from app.models.pricing import Order

    po_id = (await db.execute(select(Order.payment_order_id).where(Order.id == uuid.UUID(chg_order)))).scalar_one()
    refs = (await db.execute(select(Refund).where(Refund.payment_order_id == po_id))).scalars().all()
    assert len(refs) == 1 and refs[0].status == "AWAITING_APPROVAL"
    hs = {h.id: (h.status, h.release_reason) for h in await holds(db, cid)}
    assert hs[old.id] == ("CONVERTED", None)
    assert any(v == ("CANCELLED", "REFUNDED") for k, v in hs.items() if k != old.id)
    assert await booked(db, cid) == {old.id: 2}


async def test_sem_capacidade_na_aprovacao_erro_claro_e_reserva_anterior_intacta(db, client):
    t, org, ests, screens, s, sa, cid = await live(db, client)
    await approval(db, "MANUAL")
    old = (await holds(db, cid))[0]
    # alteração: 10h–12h com 4/h (mais barata que 2/h o dia todo ⇒ sem cobrança, sem reserva na submissão)
    r = await client.post(f"/api/v1/campaigns/{cid}/changes", headers=s.headers,
                          json={"time_bands": [[600, 720]], "freq_value": 4})
    assert r.status_code == 201 and r.json()["diff_cents"] <= 0, r.text
    # outro anunciante ocupa 10h–12h com 120 s × 13/h (13 blocos inteiros; os 2 blocos com as nossas inserções
    # vigentes ficam com 3 unidades livres) ⇒ a nova configuração (4/h, uma por bloco) não cabe
    org_b = await make_org(db)
    await set_payer(db, org_b.id)
    ub = await make_user(db, role="advertiser", scope=("ORGANIZATION", org_b.id))
    sb = await login_as(client, ub)
    cid_b = await create_campaign(client, sb, db, org_b, start=await today_local(db), days=7, freq=13,
                                  duration=120, bands=[[600, 720]], name="Outro")
    rb = await client.post(f"/api/v1/campaigns/{cid_b}/checkout",
                           headers={**sb.headers, "Idempotency-Key": f"b-{uuid.uuid4().hex}"}, json={})
    assert rb.status_code == 201, rb.text
    r = await client.post(f"/api/v1/campaigns/{cid}/approve", headers=sa.headers, json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "CHANGE_NO_CAPACITY", r.text
    c = await camp(db, cid)
    assert c.status == "EM_ANALISE" and c.pending_revision_id is not None
    assert [(h.id, h.status) for h in await holds(db, cid)] == [(old.id, "CONVERTED")]
    assert await booked(db, cid) == {old.id: 2}
    assert cid in await manifest_campaigns(db, screens[0].id)
    n = (await db.execute(text("SELECT count(*) FROM notifications WHERE event_type = 'campaign.change_no_capacity' "
                               "AND org_id = :o"), {"o": org.id})).scalar_one()
    assert n >= 1
    rows = (await db.execute(text("SELECT count(*) FROM audit_logs WHERE action = 'campaign.change_approval_failed' "
                                  "AND entity_id = :e"), {"e": cid})).scalar_one()
    assert rows == 1
